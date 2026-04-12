--
-- Licensed to the Apache Software Foundation (ASF) under one or more
-- contributor license agreements. See the NOTICE file distributed with
-- this work for additional information regarding copyright ownership.
-- The ASF licenses this file to You under the Apache License, Version 2.0
-- (the "License"); you may not use this file except in compliance with
-- the License. You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.
--

-- =============================================================================
-- ai-rate-limiting  (v5 — overrides upstream plugin; instance TPM optional;
--                         tenant TPM triggers fallback)
--
-- Deployment: place this file in a custom plugin directory and set
--   extra_lua_path in config.yaml so Lua finds it before the built-in plugin.
--   Because the file name and plugin_name match "ai-rate-limiting", the native
--   plugin is transparently replaced — ai-proxy-multi's fallback_strategy
--   ["rate_limiting"] works without any patch to ai-proxy-multi.lua.
--
-- Changes vs v4 (ai-rate-limiting-redis):
--   • plugin_name = "ai-rate-limiting"  (was "ai-rate-limiting-redis")
--     Redis key prefix changes to "plugin-ai-rate-limiting..."
--   • Instance-level TPM (instances / limit+time_window) is now OPTIONAL.
--     tenant_tpm alone is a valid configuration.
--   • check_instance_status now checks BOTH instance and tenant TPM.
--     When tenant TPM is exhausted on instance A, check_instance_status
--     returns false → ai-proxy-multi falls back to instance B, where the
--     same tenant may still have quota (independent per-instance counter).
--     No changes to ai-proxy-multi.lua required.
--   • Access phase Step 1 is skipped (not an error) when no instance-level
--     limit_conf exists for the current AI instance.
--   • Log phase continues to the tenant counter update even when there is no
--     instance-level counter to update.
--   • anyOf schema extended with { required = {"tenant_tpm"} }.
--
-- Changes vs v3 (post-hoc token accounting):
--   • Access phase uses cost=0 (Redis INCRBY key 0) — pure read, no write.
--   • Log phase deducts the full actual token count.
--   • No +1 placeholder, no -1 restore logic anywhere.
--   • check_instance_status also uses cost=0.
-- =============================================================================

local require        = require
local setmetatable   = setmetatable
local ipairs         = ipairs
local type           = type
local core           = require("apisix.core")
local limit_count    = require("apisix.plugins.limit-count.init")
local redis_schema   = require("apisix.utils.redis-schema")
local policy_to_additional_properties = redis_schema.schema

local plugin_name = "ai-rate-limiting"

-- ---------------------------------------------------------------------------
-- Schema helpers
-- ---------------------------------------------------------------------------

local instance_limit_schema = {
    type = "object",
    properties = {
        name        = { type = "string"  },
        limit       = { type = "integer", minimum = 1 },
        time_window = { type = "integer", minimum = 1 },
    },
    required = { "name", "limit", "time_window" },
}

-- A single per-instance entry used inside tenant_tpm arrays.
-- `name` must match the AI instance name (e.g. "azure-open-ai").
local tenant_limit_entry_schema = {
    type = "object",
    properties = {
        name        = { type = "string", minLength = 1 },
        limit       = { type = "integer", minimum = 1 },
        time_window = { type = "integer", minimum = 1 },
    },
    required = { "name", "limit", "time_window" },
}

local tenant_limit_list_schema = {
    type     = "array",
    items    = tenant_limit_entry_schema,
    minItems = 1,
}

-- tenant_tpm block:
--   default   – array of per-instance limits for every tenant without override
--   overrides – map of tenant_id -> array of per-instance limits
local tenant_tpm_schema = {
    type = "object",
    properties = {
        default   = tenant_limit_list_schema,
        overrides = {
            type                 = "object",
            additionalProperties = tenant_limit_list_schema,
            description          = "Per-tenant overrides keyed by tenant ID (e.g. 't-12345678')",
        },
    },
    required = { "default" },
}

local schema = {
    type = "object",
    properties = {
        -- Instance-level TPM (optional – either flat limit or per-instance list)
        limit       = { type = "integer", exclusiveMinimum = 0 },
        time_window = { type = "integer", exclusiveMinimum = 0 },

        show_limit_quota_header = { type = "boolean", default = true },

        limit_strategy = {
            type    = "string",
            enum    = { "total_tokens", "prompt_tokens", "completion_tokens" },
            default = "total_tokens",
            description = "Which token field to count against the limit",
        },

        instances = {
            type     = "array",
            items    = instance_limit_schema,
            minItems = 1,
            description = "Per-instance TPM limits (optional)",
        },

        -- Tenant-level TPM (optional – but one of instances/limit/tenant_tpm required)
        tenant_tpm = tenant_tpm_schema,

        rejected_code = {
            type    = "integer",
            minimum = 200,
            maximum = 599,
            default = 503,
        },
        rejected_msg = {
            type      = "string",
            minLength = 1,
        },

        policy = {
            type    = "string",
            enum    = { "local", "redis" },
            default = "local",
        },

        allow_degradation = { type = "boolean", default = true },
    },
    dependencies = {
        limit       = { "time_window" },
        time_window = { "limit" },
    },
    -- At least one limiting dimension must be configured.
    anyOf = {
        { required = { "limit", "time_window" } },
        { required = { "instances" } },
        { required = { "tenant_tpm" } },
    },
    ["if"]   = { properties = { policy = { enum = { "redis" } } } },
    ["then"] = policy_to_additional_properties.redis,
}

local _M = {
    version  = 0.1,
    priority = 1029,
    name     = plugin_name,
    schema   = schema,
}

-- ---------------------------------------------------------------------------
-- LRU caches
-- ---------------------------------------------------------------------------

-- Keyed by conf table identity (one entry per route/plugin config).
local limit_conf_cache = core.lrucache.new({ ttl = 300, count = 512 })

-- Keyed by "<conf_id>#<instance_name>#<tenant_id>" string.
-- count=4096 handles up to 4096 active (config, instance, tenant) combinations.
local tenant_limit_conf_cache = core.lrucache.new({ ttl = 300, count = 4096 })

-- ---------------------------------------------------------------------------
-- Schema validation
-- ---------------------------------------------------------------------------

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

-- ---------------------------------------------------------------------------
-- Internal helpers
-- ---------------------------------------------------------------------------

-- Stable string identifier for the plugin_conf, used as the Redis key namespace.
-- Uses _meta.id (APISIX route plugin config ID) when available so keys survive
-- process restarts.  Falls back to "default" for edge cases (local config).
local function get_plugin_conf_id(plugin_conf)
    return (plugin_conf._meta and plugin_conf._meta.id) or "default"
end

-- Copy Redis connection fields from plugin_conf into a limit_conf table.
local function copy_redis_conf(plugin_conf, conf)
    conf.redis_host               = plugin_conf.redis_host
    conf.redis_port               = plugin_conf.redis_port
    conf.redis_username           = plugin_conf.redis_username
    conf.redis_password           = plugin_conf.redis_password
    conf.redis_database           = plugin_conf.redis_database
    conf.redis_timeout            = plugin_conf.redis_timeout
    conf.redis_ssl                = plugin_conf.redis_ssl
    conf.redis_ssl_verify         = plugin_conf.redis_ssl_verify
    conf.redis_keepalive_timeout  = plugin_conf.redis_keepalive_timeout
    conf.redis_keepalive_pool     = plugin_conf.redis_keepalive_pool
end

-- Build a limit_conf for an AI instance.
--
-- conf.group bypasses limit-count's gen_limit_key parent.resource_key check.
-- Final Redis key: "plugin-ai-rate-limiting<conf_id>#<instance_key>:<instance_key>"
local function transform_limit_conf(plugin_conf, instance_conf, instance_name)
    local key         = plugin_name .. "#global"
    local limit       = plugin_conf.limit
    local time_window = plugin_conf.time_window
    local name        = instance_name or ""

    if instance_conf then
        name        = instance_conf.name
        key         = instance_conf.name
        limit       = instance_conf.limit
        time_window = instance_conf.time_window
    end

    local group = get_plugin_conf_id(plugin_conf) .. "#" .. key

    local conf = {
        _vid      = key,
        conf_id   = plugin_conf._meta and plugin_conf._meta.id or key,
        group     = group,
        key       = key,
        meta      = plugin_conf._meta,
        count     = limit,
        time_window         = time_window,
        rejected_code       = plugin_conf.rejected_code,
        rejected_msg        = plugin_conf.rejected_msg,
        show_limit_quota_header = plugin_conf.show_limit_quota_header,
        policy              = plugin_conf.policy or "local",
        key_type            = "constant",
        allow_degradation   = plugin_conf.allow_degradation or false,
        sync_interval       = -1,
        limit_header        = "X-AI-RateLimit-Limit-"     .. name,
        remaining_header    = "X-AI-RateLimit-Remaining-" .. name,
        reset_header        = "X-AI-RateLimit-Reset-"     .. name,
    }

    if plugin_conf.policy == "redis" then
        copy_redis_conf(plugin_conf, conf)
    end

    return conf
end

-- Search an array of { name, limit, time_window } entries for a given instance.
-- Returns the matching entry or nil.
local function find_instance_limit_in_list(list, instance_name)
    for _, entry in ipairs(list) do
        if entry.name == instance_name then
            return entry
        end
    end
    return nil
end

-- Build a limit_conf for a specific (instance, tenant) pair.
--
-- Lookup order:
--   1. overrides[tenant_id] array → entry where name == instance_name
--   2. default array              → entry where name == instance_name
--   3. nil → no tenant check for this combination
--
-- Final Redis key: "plugin-ai-rate-limiting<conf_id>#<instance>#tenant#<tid>:<instance>#tenant#<tid>"
local function build_tenant_limit_conf(plugin_conf, instance_name, tenant_id)
    local tenant_tpm = plugin_conf.tenant_tpm

    local override_list = tenant_tpm.overrides and tenant_tpm.overrides[tenant_id]
    local limit_cfg = (override_list and find_instance_limit_in_list(override_list, instance_name))
                      or find_instance_limit_in_list(tenant_tpm.default, instance_name)

    if not limit_cfg then
        return nil
    end

    local key   = instance_name .. "#tenant#" .. tenant_id
    local group = get_plugin_conf_id(plugin_conf) .. "#" .. key

    local conf = {
        _vid      = key,
        conf_id   = get_plugin_conf_id(plugin_conf) .. "#" .. key,
        group     = group,
        key       = key,
        meta      = plugin_conf._meta,
        count       = limit_cfg.limit,
        time_window = limit_cfg.time_window,
        rejected_code       = plugin_conf.rejected_code,
        rejected_msg        = plugin_conf.rejected_msg,
        show_limit_quota_header = plugin_conf.show_limit_quota_header,
        policy              = plugin_conf.policy or "local",
        key_type            = "constant",
        allow_degradation   = plugin_conf.allow_degradation or false,
        sync_interval       = -1,
        limit_header     = "X-AI-RateLimit-Limit-Tenant",
        remaining_header = "X-AI-RateLimit-Remaining-Tenant",
        reset_header     = "X-AI-RateLimit-Reset-Tenant",
    }

    if plugin_conf.policy == "redis" then
        copy_redis_conf(plugin_conf, conf)
    end

    return conf
end

-- Retrieve (or build + cache) the tenant limit_conf for a given
-- (instance_name, tenant_id) pair.
local function get_tenant_limit_conf(plugin_conf, instance_name, tenant_id)
    local conf_id   = get_plugin_conf_id(plugin_conf)
    local cache_key = conf_id .. "#" .. instance_name .. "#" .. tenant_id
    return tenant_limit_conf_cache(
        cache_key, nil,
        build_tenant_limit_conf, plugin_conf, instance_name, tenant_id
    )
end

-- Build the kvs map for all AI instances.
-- Returns an empty-ish table (with __index fallback) when only tenant_tpm is
-- configured and no per-instance limits exist.
local function fetch_limit_conf_kvs(conf)
    local mt = {
        __index = function(t, k)
            if not conf.limit then
                return nil
            end
            local limit_conf = transform_limit_conf(conf, nil, k)
            t[k] = limit_conf
            return limit_conf
        end
    }
    local limit_conf_kvs  = setmetatable({}, mt)
    local conf_instances  = conf.instances or {}
    for _, limit_conf in ipairs(conf_instances) do
        limit_conf_kvs[limit_conf.name] = transform_limit_conf(conf, limit_conf)
    end
    return limit_conf_kvs
end

-- ---------------------------------------------------------------------------
-- Access phase
-- ---------------------------------------------------------------------------

function _M.access(conf, ctx)
    local ai_instance_name = ctx.picked_ai_instance_name
    if not ai_instance_name then
        return
    end

    -- -----------------------------------------------------------------------
    -- Step 1: Instance-level TPM check (OPTIONAL)
    --
    -- Skipped when no per-instance limit is configured (limit_conf == nil).
    -- cost=0 → Redis INCRBY key 0: pure read, counter is never written in the
    -- access phase.  Actual token deduction happens in the log phase.
    -- -----------------------------------------------------------------------
    local limit_conf_kvs = limit_conf_cache(conf, nil, fetch_limit_conf_kvs, conf)
    local limit_conf     = limit_conf_kvs[ai_instance_name]

    if limit_conf then
        local code, msg = limit_count.rate_limit(limit_conf, ctx, plugin_name, 0, false)
        if code then
            core.log.info(
                "instance TPM exceeded: ", ai_instance_name, " code: ", code
            )
            ctx.ai_rate_limiting = true
            return code, msg
        end
    end

    -- -----------------------------------------------------------------------
    -- Step 2: Tenant-level TPM check (OPTIONAL, per-instance per-tenant)
    --
    -- Executed when tenant_tpm is configured AND t-tenant-id header is present.
    -- cost=0: same pure-read approach as Step 1.
    -- -----------------------------------------------------------------------
    if conf.tenant_tpm then
        local tenant_id = ngx.var.http_t_tenant_id
        if tenant_id and tenant_id ~= "" then
            local tenant_limit_conf = get_tenant_limit_conf(conf, ai_instance_name, tenant_id)

            if tenant_limit_conf then
                local t_code, t_msg = limit_count.rate_limit(
                    tenant_limit_conf, ctx, plugin_name, 0, false
                )
                if t_code then
                    core.log.info(
                        "tenant TPM exceeded: ", tenant_id,
                        " instance: ", ai_instance_name,
                        " code: ", t_code
                    )
                    ctx.ai_rate_limiting = true
                    return t_code, t_msg or ("tenant " .. tenant_id .. " TPM rate limit exceeded")
                end
            else
                core.log.info(
                    "no tenant_tpm entry for instance: ", ai_instance_name,
                    " tenant: ", tenant_id, ", skipping tenant check"
                )
            end

            ctx.ai_tenant_id = tenant_id
        end
    end

    ctx.ai_rate_limiting = false
end

-- ---------------------------------------------------------------------------
-- check_instance_status  (called by ai-proxy-multi for fallback_strategy)
-- ---------------------------------------------------------------------------
--
-- Returns true  → instance is available (all checked counters below limit)
-- Returns false → instance or tenant TPM exhausted for this instance
-- Returns nil, err → configuration error
--
-- Checks both dimensions so either can trigger ai-proxy-multi fallback:
--   1. Instance-level TPM (if instances / limit+time_window configured)
--   2. Tenant-level TPM   (if tenant_tpm configured AND t-tenant-id header present)
--
-- Because tenant TPM is keyed per (instance, tenant), exhausting the tenant
-- quota on "openai-p1-a" does NOT exhaust it on "openai-p1-b" — the fallback
-- to another instance is meaningful and allows the tenant to be served by a
-- different backend.
--
-- All checks use cost=0: pure Redis read (INCRBY key 0), no counter write.

function _M.check_instance_status(conf, ctx, instance_name)
    if conf == nil then
        local plugins = ctx.plugins
        for i = 1, #plugins, 2 do
            if plugins[i]["name"] == plugin_name then
                conf = plugins[i + 1]
                break
            end
        end
    end

    if not conf then
        return true
    end

    instance_name = instance_name or ctx.picked_ai_instance_name
    if not instance_name then
        return nil, "missing instance_name"
    end

    if type(instance_name) ~= "string" then
        return nil, "invalid instance_name"
    end

    -- Step 1: Instance-level TPM (skipped when not configured)
    local limit_conf_kvs = limit_conf_cache(conf, nil, fetch_limit_conf_kvs, conf)
    local limit_conf     = limit_conf_kvs[instance_name]
    if limit_conf then
        local code, _ = limit_count.rate_limit(limit_conf, ctx, plugin_name, 0, false)
        if code then
            core.log.info(
                "check_instance_status: instance TPM exhausted for ", instance_name
            )
            return false
        end
    end

    -- Step 2: Tenant-level TPM (skipped when not configured or no header)
    -- tenant_id is readable from the request header at this point in the
    -- access phase — ai-proxy-multi calls us before ctx.picked_ai_instance_name
    -- is set, but ngx.var is already available.
    if conf.tenant_tpm then
        local tenant_id = ngx.var.http_t_tenant_id
        if tenant_id and tenant_id ~= "" then
            local tenant_limit_conf = get_tenant_limit_conf(conf, instance_name, tenant_id)
            if tenant_limit_conf then
                local t_code, _ = limit_count.rate_limit(
                    tenant_limit_conf, ctx, plugin_name, 0, false
                )
                if t_code then
                    core.log.info(
                        "check_instance_status: tenant TPM exhausted",
                        " instance: ", instance_name, " tenant: ", tenant_id
                    )
                    return false
                end
            end
        end
    end

    return true
end

-- ---------------------------------------------------------------------------
-- Log phase
-- ---------------------------------------------------------------------------

local function get_token_usage(conf, ctx)
    local usage = ctx.ai_token_usage
    if not usage then
        return
    end
    return usage[conf.limit_strategy]
end

function _M.log(conf, ctx)
    local instance_name = ctx.picked_ai_instance_name
    if not instance_name then
        return
    end

    if ctx.ai_rate_limiting then
        return
    end

    local used_tokens = get_token_usage(conf, ctx)
    if not used_tokens then
        core.log.error("failed to get token usage for llm service")
        return
    end

    core.log.info("instance: ", instance_name, " used_tokens: ", used_tokens)

    -- Prepare instance timer_conf (may be nil when instance-level TPM not configured).
    local limit_conf_kvs = limit_conf_cache(conf, nil, fetch_limit_conf_kvs, conf)
    local limit_conf     = limit_conf_kvs[instance_name]

    local timer_limit_conf
    if limit_conf then
        timer_limit_conf = core.table.deepcopy(limit_conf)
        timer_limit_conf.show_limit_quota_header = false
    end

    -- Prepare tenant timer_conf (may be nil when tenant_tpm not configured or
    -- no entry matches this (instance, tenant) combination).
    local timer_tenant_limit_conf
    local tenant_id = ctx.ai_tenant_id
    if conf.tenant_tpm and tenant_id then
        local tenant_limit_conf = get_tenant_limit_conf(conf, instance_name, tenant_id)
        if tenant_limit_conf then
            timer_tenant_limit_conf = core.table.deepcopy(tenant_limit_conf)
            timer_tenant_limit_conf.show_limit_quota_header = false
        end
    end

    -- Nothing to update.
    if not timer_limit_conf and not timer_tenant_limit_conf then
        return
    end

    local timer_ctx = {
        var          = ctx.var,
        route_id     = ctx.route_id,
        route_name   = ctx.route_name,
        plugins      = ctx.plugins,
        conf_type    = ctx.conf_type,
        conf_version = ctx.conf_version,
        conf_id      = ctx.conf_id,
    }

    local ok, err = ngx.timer.at(0, function(premature)
        if premature then
            return
        end

        -- Add full actual token count to the instance counter.
        if timer_limit_conf then
            local code, msg = limit_count.rate_limit(
                timer_limit_conf, timer_ctx, plugin_name, used_tokens
            )
            if code then
                core.log.warn("instance token update failed: ", msg)
            else
                core.log.info("deducted ", used_tokens, " tokens for instance: ", instance_name)
            end
        end

        -- Add full actual token count to the tenant counter.
        if timer_tenant_limit_conf then
            local t_code, t_msg = limit_count.rate_limit(
                timer_tenant_limit_conf, timer_ctx, plugin_name, used_tokens
            )
            if t_code then
                core.log.warn("tenant token update failed: ", t_msg)
            else
                core.log.info("deducted ", used_tokens, " tokens for tenant: ", tenant_id)
            end
        end
    end)

    if not ok then
        core.log.error("failed to create timer for token usage update: ", err)
    end
end

return _M
