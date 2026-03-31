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
-- ai-rate-limiting-redis  (v3 — per-instance tenant TPM arrays)
--
-- Changes vs v2:
--   • `tenant_tpm.default`   is now an ARRAY of { name, limit, time_window }
--     instead of a single flat object – each entry targets one AI instance.
--   • `tenant_tpm.overrides` values are now arrays of the same shape.
--   • Added `find_instance_limit_in_list()` helper for array lookup.
--   • `build_tenant_limit_conf` returns nil when no entry matches the
--     current (instance_name, tenant_id) pair – access/log phases skip
--     tenant limiting in that case rather than applying a wrong limit.
--
-- Changes vs v1:
--   • Added  `tenant_tpm`  config block  (default limit + per-tenant overrides)
--   • Access phase: after instance check, also checks the tenant TPM budget
--   • Log   phase: the ngx.timer also adjusts the tenant counter
--   • New helper: `build_tenant_limit_conf` / `get_tenant_limit_conf`
--   • All other code paths are UNCHANGED to minimise diff with upstream.
-- =============================================================================

local require        = require
local setmetatable   = setmetatable
local ipairs         = ipairs
local type           = type
local core           = require("apisix.core")
local limit_count    = require("apisix.plugins.limit-count.init")
local redis_schema   = require("apisix.utils.redis-schema")
local policy_to_additional_properties = redis_schema.schema

local plugin_name = "ai-rate-limiting-redis"

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

-- NEW: a single per-instance entry: { name, limit, time_window }
--   `name` matches the AI instance name (e.g. "azure-open-ai")
local tenant_limit_entry_schema = {
    type = "object",
    properties = {
        name        = { type = "string", minLength = 1 },
        limit       = { type = "integer", minimum = 1 },
        time_window = { type = "integer", minimum = 1 },
    },
    required = { "name", "limit", "time_window" },
}

-- Array of per-instance tenant TPM entries
local tenant_limit_list_schema = {
    type     = "array",
    items    = tenant_limit_entry_schema,
    minItems = 1,
}

-- NEW: tenant_tpm block
--   default   – array of per-instance limits applied to every tenant without override
--   overrides – map of tenant_id -> array of per-instance limits
local tenant_tpm_schema = {
    type = "object",
    properties = {
        default   = tenant_limit_list_schema,
        overrides = {
            type                 = "object",
            additionalProperties = tenant_limit_list_schema,
            description          = "Per-tenant TPM overrides keyed by tenant ID (e.g. 't-12345678')",
        },
    },
    required = { "default" },
}

local schema = {
    type = "object",
    properties = {
        limit       = { type = "integer", exclusiveMinimum = 0 },
        time_window = { type = "integer", exclusiveMinimum = 0 },

        show_limit_quota_header = { type = "boolean", default = true },

        limit_strategy = {
            type    = "string",
            enum    = { "total_tokens", "prompt_tokens", "completion_tokens" },
            default = "total_tokens",
            description = "The strategy to limit the tokens",
        },

        instances = {
            type     = "array",
            items    = instance_limit_schema,
            minItems = 1,
        },

        -- NEW ---------------------------------------------------------------
        tenant_tpm = tenant_tpm_schema,
        -- -------------------------------------------------------------------

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
    anyOf = {
        { required = { "limit", "time_window" } },
        { required = { "instances" } },
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

-- Existing: keyed by conf table identity (one entry per route/plugin config)
local limit_conf_cache = core.lrucache.new({ ttl = 300, count = 512 })

-- NEW: keyed by "<conf_id>#<tenant_id>" string – needs more slots because
--      the number of active tenants can be large.
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

-- Return a stable string identifier for the plugin_conf, used as the Redis key
-- namespace (via conf.group).  Must be stable across process restarts.
-- Falls back to "default" when _meta.id is absent (edge case / local config).
local function get_plugin_conf_id(plugin_conf)
    return (plugin_conf._meta and plugin_conf._meta.id) or "default"
end

-- Copy Redis connection fields from plugin_conf into a limit_conf table.
-- Extracted to avoid duplication between instance and tenant conf builders.
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

-- Build a limit_conf for an AI instance (unchanged logic from v1,
-- only redis copy factored into copy_redis_conf).
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

    -- conf.group bypasses limit-count's gen_limit_key parent.resource_key requirement.
    -- Format: "<plugin_conf_id>#<instance_key>"
    -- Final Redis key: "plugin-ai-rate-limiting-redis<group>:<key>"
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

-- NEW: Search an array of { name, limit, time_window } entries for a specific
--      instance name.  Returns the matching entry or nil if not found.
local function find_instance_limit_in_list(list, instance_name)
    for _, entry in ipairs(list) do
        if entry.name == instance_name then
            return entry
        end
    end
    return nil
end

-- NEW: Build a limit_conf for a specific (instance, tenant) pair.
--
--   conf.group format:  "<plugin_conf_id>#<instance_name>#tenant#<tenant_id>"
--   Final Redis key:    "plugin-ai-rate-limiting-redis<group>:<key>"
--
--   This ensures each (instance, tenant) combination has its own independent
--   counter: tenant t-12345678 on openai-primary and on deepseek-backup are
--   tracked separately.
--
--   Lookup order:
--     1. overrides[tenant_id] list  → entry matching instance_name
--     2. default list               → entry matching instance_name
--     3. nil (no config for this instance → no tenant check)
local function build_tenant_limit_conf(plugin_conf, instance_name, tenant_id)
    local tenant_tpm = plugin_conf.tenant_tpm

    -- Pick the right list: override list for this tenant, or the default list
    local override_list = tenant_tpm.overrides and tenant_tpm.overrides[tenant_id]
    local limit_cfg = (override_list and find_instance_limit_in_list(override_list, instance_name))
                      or find_instance_limit_in_list(tenant_tpm.default, instance_name)

    -- No entry configured for this (instance, tenant) combination → skip limiting
    if not limit_cfg then
        return nil
    end

    -- Encode both dimensions into the key so Redis counters are independent
    -- per (instance, tenant) pair.
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
        -- Dedicated response headers so tenant quota is visible separately
        -- from instance quota.
        limit_header     = "X-AI-RateLimit-Limit-Tenant",
        remaining_header = "X-AI-RateLimit-Remaining-Tenant",
        reset_header     = "X-AI-RateLimit-Reset-Tenant",
    }

    if plugin_conf.policy == "redis" then
        copy_redis_conf(plugin_conf, conf)
    end

    return conf
end

-- NEW: Retrieve (or build + cache) the tenant limit_conf for a given
--      (instance_name, tenant_id) pair.
local function get_tenant_limit_conf(plugin_conf, instance_name, tenant_id)
    local conf_id   = get_plugin_conf_id(plugin_conf)
    -- Cache key must include both instance and tenant to avoid cross-contamination.
    local cache_key = conf_id .. "#" .. instance_name .. "#" .. tenant_id
    return tenant_limit_conf_cache(
        cache_key, nil,
        build_tenant_limit_conf, plugin_conf, instance_name, tenant_id
    )
end

-- Build the kvs map for all AI instances (unchanged from v1).
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

    local limit_conf_kvs = limit_conf_cache(conf, nil, fetch_limit_conf_kvs, conf)
    local limit_conf     = limit_conf_kvs[ai_instance_name]
    if not limit_conf then
        return
    end

    -- -----------------------------------------------------------------------
    -- Step 1: Instance-level check (unchanged from v1)
    -- -----------------------------------------------------------------------
    local code, msg = limit_count.rate_limit(limit_conf, ctx, plugin_name, 1, true)
    if code then
        limit_count.rate_limit(limit_conf, ctx, plugin_name, -1, false)
        core.log.info(
            "rate limit exceeded in access phase for instance: ", ai_instance_name,
            " code: ", code, ", placeholder quota restored"
        )
        ctx.ai_rate_limiting = true
        return code, msg
    end

    -- -----------------------------------------------------------------------
    -- Step 2: Tenant-level check (NEW)
    --
    -- Only executed when:
    --   a) `tenant_tpm` is configured in the plugin config, AND
    --   b) the incoming request carries a non-empty `t-tenant-id` header.
    --
    -- If the tenant limit is exceeded the instance placeholder reserved in
    -- Step 1 is also restored to avoid counter drift.
    -- -----------------------------------------------------------------------
    if conf.tenant_tpm then
        local tenant_id = ngx.var.http_t_tenant_id
        if tenant_id and tenant_id ~= "" then
            -- Counter is per (instance, tenant): each AI instance tracks its
            -- own tenant TPM independently.
            local tenant_limit_conf = get_tenant_limit_conf(conf, ai_instance_name, tenant_id)

            if tenant_limit_conf then
                -- An entry exists for this (instance, tenant) → enforce it.
                local t_code, t_msg = limit_count.rate_limit(
                    tenant_limit_conf, ctx, plugin_name, 1, true
                )

                if t_code then
                    -- Tenant budget exhausted – restore both placeholders.
                    limit_count.rate_limit(limit_conf,        ctx, plugin_name, -1, false)
                    limit_count.rate_limit(tenant_limit_conf, ctx, plugin_name, -1, false)

                    core.log.info(
                        "tenant rate limit exceeded in access phase",
                        " tenant: ",   tenant_id,
                        " instance: ", ai_instance_name,
                        " code: ",     t_code,
                        ", both placeholders restored"
                    )
                    ctx.ai_rate_limiting = true
                    return t_code, t_msg or ("tenant " .. tenant_id .. " TPM rate limit exceeded")
                end
            else
                -- No config entry for this (instance_name, tenant_id) pair → skip.
                core.log.info(
                    "no tenant_tpm entry for instance: ", ai_instance_name,
                    " tenant: ", tenant_id, ", skipping tenant check"
                )
            end

            -- Store tenant_id so the log phase can find the right counter.
            ctx.ai_tenant_id = tenant_id
        end
    end

    ctx.ai_rate_limiting = false
end

-- ---------------------------------------------------------------------------
-- check_instance_status  (unchanged from v1 – no tenant dimension here)
-- ---------------------------------------------------------------------------

function _M.check_instance_status(conf, ctx, instance_name)
    if conf == nil then
        local plugins = ctx.plugins
        for i = 1, #plugins, 2 do
            if plugins[i]["name"] == plugin_name then
                conf = plugins[i + 1]
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

    local limit_conf_kvs = limit_conf_cache(conf, nil, fetch_limit_conf_kvs, conf)
    local limit_conf     = limit_conf_kvs[instance_name]
    if not limit_conf then
        return true
    end

    local code, _ = limit_count.rate_limit(limit_conf, ctx, plugin_name, 1, true)
    if code then
        limit_count.rate_limit(limit_conf, ctx, plugin_name, -1, false)
        core.log.info(
            "rate limit for instance: ", instance_name,
            " code: ", code, ", quota restored"
        )
        return false
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

    core.log.info("instance name: ", instance_name, " used tokens: ", used_tokens)

    local limit_conf_kvs = limit_conf_cache(conf, nil, fetch_limit_conf_kvs, conf)
    local limit_conf     = limit_conf_kvs[instance_name]
    if not limit_conf then
        return
    end

    -- extra_tokens = actual usage - placeholder already reserved in access phase
    local extra_tokens = used_tokens - 1

    if extra_tokens == 0 then
        core.log.info("token usage equals placeholder, no adjustment needed")
        return
    end

    -- Disable header writing inside timer (socket API not available in log phase)
    local timer_limit_conf = core.table.deepcopy(limit_conf)
    timer_limit_conf.show_limit_quota_header = false

    -- NEW: prepare tenant limit_conf for the timer if a tenant was identified.
    --      Must use the same (instance_name, tenant_id) pair as the access phase.
    local timer_tenant_limit_conf
    local tenant_id = ctx.ai_tenant_id
    if conf.tenant_tpm and tenant_id then
        local tenant_limit_conf = get_tenant_limit_conf(conf, instance_name, tenant_id)
        -- tenant_limit_conf may be nil when no entry is configured for this
        -- (instance_name, tenant_id) combination – in that case skip adjustment.
        if tenant_limit_conf then
            timer_tenant_limit_conf = core.table.deepcopy(tenant_limit_conf)
            timer_tenant_limit_conf.show_limit_quota_header = false
        end
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

        -- Adjust instance counter (unchanged from v1)
        local code, msg = limit_count.rate_limit(
            timer_limit_conf, timer_ctx, plugin_name, extra_tokens
        )
        if code then
            core.log.warn("failed to update instance token usage in background: ", msg)
        else
            if extra_tokens > 0 then
                core.log.info(
                    "deducted ", extra_tokens,
                    " additional tokens for instance: ", instance_name
                )
            else
                core.log.info(
                    "restored ", -extra_tokens,
                    " tokens for instance: ", instance_name
                )
            end
        end

        -- NEW: Adjust tenant counter with the same delta
        if timer_tenant_limit_conf then
            local t_code, t_msg = limit_count.rate_limit(
                timer_tenant_limit_conf, timer_ctx, plugin_name, extra_tokens
            )
            if t_code then
                core.log.warn(
                    "failed to update tenant token usage in background: ", t_msg
                )
            else
                if extra_tokens > 0 then
                    core.log.info(
                        "deducted ", extra_tokens,
                        " additional tokens for tenant: ", tenant_id
                    )
                else
                    core.log.info(
                        "restored ", -extra_tokens,
                        " tokens for tenant: ", tenant_id
                    )
                end
            end
        end
    end)

    if not ok then
        core.log.error("failed to create timer for token usage update: ", err)
    end
end

return _M
