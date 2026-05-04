-- =============================================================================
-- test_ai_rate_limiting.lua
-- Verifies that the 503→429 fix does NOT break the fallback mechanism.
--
-- Scenarios:
--   T1  Normal: instance available → true, no flag set
--   T2  Fallback: A exhausted → false + flag; B available → true (fallback works)
--   T3  All exhausted: header_filter converts 503 → 429
--   T4  Fallback success: flag set but status=200 → header_filter does nothing
--   T5  Non-TPM 503: flag absent → header_filter leaves 503 untouched
--   T6  body_filter: replaces body only when status was overridden
--   T7  body_filter: leaves body untouched when no override
--   T8  Tenant fallback: tenant exhausted on A, available on B → true (fallback works)
-- =============================================================================

-- ============================================================
-- Mock: ngx global
-- ============================================================
ngx = {
    status = 200,
    arg    = {},
    var    = { http_t_tenant_id = nil },
    null   = {},
    timer  = {
        -- Execute immediately (synchronously) so log-phase counters are testable
        at = function(_, fn) fn(false); return true, nil end
    },
}

-- ============================================================
-- Mock: apisix.core
-- ============================================================

-- Simple LRU-cache stand-in: one entry per string key, no eviction.
local function make_lrucache()
    local store = {}
    return function(key, _, factory, ...)
        if store[key] == nil then
            store[key] = factory(...)
        end
        return store[key]
    end
end

local core_mock = {
    log = {
        info  = function(...) end,
        warn  = function(...) end,
        error = function(...) end,
    },
    lrucache = { new = function() return make_lrucache() end },
    schema   = { check = function() return true end },
    response = { set_header = function() end },
    table    = {
        deepcopy = function(t)
            local c = {}
            for k, v in pairs(t) do c[k] = v end
            return c
        end,
    },
    json = {
        encode = function(t)
            if type(t) == "table" and t.error_msg then
                return '{"error_msg":"' .. t.error_msg .. '"}'
            end
            return tostring(t)
        end,
    },
}

-- ============================================================
-- Mock: limit-count  (controlled per test via `exhausted_set`)
-- ============================================================
-- exhausted_set is a table of instance _vid strings that are "over limit".
-- Tests set this before calling the plugin.
local exhausted_set = {}

local limit_count_mock = {
    rate_limit = function(lconf, ctx, plugin_name, cost, dry_run)
        if exhausted_set[lconf._vid] then
            return lconf.rejected_code or 429
        end
        return nil  -- available
    end,
}

-- ============================================================
-- Mock: redis utils / schema (not needed for local-policy tests)
-- ============================================================
local redis_utils_mock  = {}
local redis_schema_mock = { schema = { redis = {} } }

-- ============================================================
-- Inject mocks before loading the plugin
-- ============================================================
package.loaded["apisix.core"]                      = core_mock
package.loaded["apisix.plugins.limit-count.init"]  = limit_count_mock
package.loaded["apisix.utils.redis"]               = redis_utils_mock
package.loaded["apisix.utils.redis-schema"]        = redis_schema_mock

local plugin = require("ai-rate-limiting")

-- ============================================================
-- Test helpers
-- ============================================================
local PASS, FAIL = 0, 0

local function check(label, got, expected)
    if got == expected then
        print(string.format("[PASS] %s", label))
        PASS = PASS + 1
    else
        print(string.format("[FAIL] %s  (got=%s  expected=%s)",
            label, tostring(got), tostring(expected)))
        FAIL = FAIL + 1
    end
end

local function make_conf(instances, tenant_tpm)
    return {
        policy              = "local",
        limit_strategy      = "total_tokens",
        show_limit_quota_header = false,
        allow_degradation   = false,
        instances           = instances,
        tenant_tpm          = tenant_tpm,
        rejected_code       = 429,
        _meta               = { id = "route-test" },
    }
end

local function make_ctx()
    return {
        var      = {},
        plugins  = {},
        route_id = "route-test",
    }
end

local function reset()
    exhausted_set = {}
    ngx.status  = 200
    ngx.arg     = {}
    ngx.var.http_t_tenant_id = nil
end

-- ============================================================
-- T1  Normal: instance available → check_instance_status = true
-- ============================================================
print("\n--- T1: Normal (instance available) ---")
reset()
do
    local conf = make_conf({ { name = "openai-a", limit = 1000, time_window = 60 } })
    local ctx  = make_ctx()
    local ok   = plugin.check_instance_status(conf, ctx, "openai-a")
    check("returns true",                    ok,                       true)
    check("ai_rl_tpm_exhausted NOT set",     ctx.ai_rl_tpm_exhausted,  nil)
end

-- ============================================================
-- T2  Fallback: A exhausted, B available → fallback works
-- ============================================================
print("\n--- T2: Fallback (A exhausted, B available) ---")
reset()
do
    exhausted_set["openai-a:1000:60"] = true   -- _vid now includes limit:time_window
    local conf = make_conf({
        { name = "openai-a", limit = 1000, time_window = 60 },
        { name = "openai-b", limit = 1000, time_window = 60 },
    })
    local ctx = make_ctx()

    -- ai-proxy-multi first tries A
    local ok_a = plugin.check_instance_status(conf, ctx, "openai-a")
    check("A: returns false (exhausted)",        ok_a,                      false)
    check("A: flag set",                          ctx.ai_rl_tpm_exhausted,  true)

    -- ai-proxy-multi falls back to B
    local ok_b = plugin.check_instance_status(conf, ctx, "openai-b")
    check("B: returns true (fallback succeeds)", ok_b,                      true)

    -- Simulate successful 200 response via B
    ngx.status = 200
    plugin.header_filter(conf, ctx)
    check("header_filter: 200 untouched (flag set but status≠503)", ngx.status, 200)
    check("ai_rl_status_overridden NOT set",  ctx.ai_rl_status_overridden,  nil)

    plugin.body_filter(conf, ctx)
    check("body_filter: body untouched on 200", ngx.arg[1], nil)
end

-- ============================================================
-- T3  All exhausted → header_filter converts 503 → 429
-- ============================================================
print("\n--- T3: All instances exhausted → 503 becomes 429 ---")
reset()
do
    exhausted_set["openai-a:1000:60"] = true
    exhausted_set["openai-b:1000:60"] = true
    local conf = make_conf({
        { name = "openai-a", limit = 1000, time_window = 60 },
        { name = "openai-b", limit = 1000, time_window = 60 },
    })
    local ctx = make_ctx()

    plugin.check_instance_status(conf, ctx, "openai-a")
    plugin.check_instance_status(conf, ctx, "openai-b")
    check("flag set after all exhausted", ctx.ai_rl_tpm_exhausted, true)

    -- ai-proxy-multi emits 503 "all servers tried"
    ngx.status = 503
    plugin.header_filter(conf, ctx)
    check("status changed to 429",          ngx.status,                429)
    check("ai_rl_status_overridden set",    ctx.ai_rl_status_overridden, true)

    plugin.body_filter(conf, ctx)
    check("body = TPM quota message",
        ngx.arg[1], "The token per minute (TPM) quota has been reached.")
    check("EOS flag set", ngx.arg[2], true)
end

-- ============================================================
-- T4  flag set but response is 200 → filters are silent
-- ============================================================
print("\n--- T4: Flag set but 200 response → filters are no-ops ---")
reset()
do
    local conf = make_conf({})
    local ctx  = make_ctx()
    ctx.ai_rl_tpm_exhausted = true   -- A was checked and exhausted, but B succeeded
    ngx.status = 200

    plugin.header_filter(conf, ctx)
    check("status stays 200",              ngx.status,                  200)
    check("override flag NOT set",         ctx.ai_rl_status_overridden, nil)

    ngx.arg = { "real response body", false }
    plugin.body_filter(conf, ctx)
    check("body untouched",                ngx.arg[1],                  "real response body")
end

-- ============================================================
-- T5  Non-TPM 503 (upstream down) → 503 untouched
-- ============================================================
print("\n--- T5: Non-TPM 503 (upstream down) → left as 503 ---")
reset()
do
    local conf = make_conf({})
    local ctx  = make_ctx()
    -- ctx.ai_rl_tpm_exhausted is nil (no rate limit was hit)
    ngx.status = 503   -- genuine upstream failure

    plugin.header_filter(conf, ctx)
    check("non-TPM 503 stays 503",          ngx.status,                  503)
    check("override flag NOT set",          ctx.ai_rl_status_overridden, nil)

    ngx.arg = { '{"error":"upstream down"}', false }
    plugin.body_filter(conf, ctx)
    check("upstream error body untouched",  ngx.arg[1], '{"error":"upstream down"}')
end

-- ============================================================
-- T6  body_filter no-op when no override
-- ============================================================
print("\n--- T6: body_filter no-op when override not set ---")
reset()
do
    local conf = make_conf({})
    local ctx  = make_ctx()
    ngx.arg = { "some body", false }
    plugin.body_filter(conf, ctx)
    check("body untouched", ngx.arg[1], "some body")
end

-- ============================================================
-- T7  Tenant fallback: tenant exhausted on A, available on B
-- ============================================================
print("\n--- T7: Tenant TPM — exhausted on A, available on B → fallback works ---")
reset()
do
    -- Tenant counter key format: "<instance>#tenant#<tenant_id>"
    exhausted_set["openai-a#tenant#t-001:500:60"] = true   -- _vid now includes limit:time_window
    -- openai-b#tenant#t-001:500:60 is NOT exhausted (independent counter)

    local tenant_tpm = {
        default = {
            { name = "openai-a", limit = 500, time_window = 60 },
            { name = "openai-b", limit = 500, time_window = 60 },
        },
    }
    local conf = make_conf({}, tenant_tpm)
    local ctx  = make_ctx()
    ngx.var.http_t_tenant_id = "t-001"

    local ok_a = plugin.check_instance_status(conf, ctx, "openai-a")
    check("A: tenant TPM exhausted → false",   ok_a,                     false)
    check("A: flag set",                        ctx.ai_rl_tpm_exhausted, true)

    local ok_b = plugin.check_instance_status(conf, ctx, "openai-b")
    check("B: tenant quota available → true (fallback works)", ok_b, true)

    -- Successful 200 from B
    ngx.status = 200
    plugin.header_filter(conf, ctx)
    check("200 response untouched after tenant fallback", ngx.status, 200)
end

-- ============================================================
-- T8  check_instance_status with nil conf falls back to ctx.plugins
-- ============================================================
print("\n--- T8: check_instance_status with nil conf reads from ctx.plugins ---")
reset()
do
    local conf = make_conf({ { name = "openai-a", limit = 1000, time_window = 60 } })
    local ctx  = make_ctx()
    -- Simulate how ai-proxy-multi originally called with nil conf
    ctx.plugins = { { name = "ai-rate-limiting" }, conf }

    local ok = plugin.check_instance_status(nil, ctx, "openai-a")
    check("nil conf: resolves from ctx.plugins, returns true", ok, true)
end

-- ============================================================
-- T9  _vid encodes limit: changing TPM creates a different _vid
--     so limit-count's internal limiter cache is invalidated immediately.
-- ============================================================
print("\n--- T9: _vid includes limit — TPM change produces different _vid ---")
reset()
do
    -- Simulate conf v1 (old limit = 1000)
    local conf_v1 = make_conf({ { name = "openai-a", limit = 1000, time_window = 60 } })
    local ctx     = make_ctx()
    local kvs_v1  = plugin.check_instance_status(conf_v1, ctx, "openai-a")  -- warm cache

    -- Extract _vid from the built limit_conf (reach into the lrucache via access)
    -- We verify indirectly: exhausted_set keyed by new _vid format works
    exhausted_set["openai-a:1000:60"] = true
    local ok_v1 = plugin.check_instance_status(conf_v1, ctx, "openai-a")
    check("T9: v1 (limit=1000) enforces old limit → false", ok_v1, false)

    -- Simulate admin updates limit to 2000 → APISIX creates new conf table
    reset()
    local ctx_v2  = make_ctx()   -- fresh ctx, no leftover flags from v1
    local conf_v2 = make_conf({ { name = "openai-a", limit = 2000, time_window = 60 } })
    -- exhausted_set only has the OLD _vid key; new _vid is "openai-a:2000:60" → no match
    exhausted_set["openai-a:1000:60"] = true   -- old entry, should NOT match

    local ok_v2 = plugin.check_instance_status(conf_v2, ctx_v2, "openai-a")
    check("T9: v2 (limit=2000) uses new _vid → not exhausted → true", ok_v2, true)
    check("T9: flag NOT set (different _vid, different limiter)", ctx_v2.ai_rl_tpm_exhausted, nil)
end

-- ============================================================
-- Summary
-- ============================================================
print("\n" .. string.rep("=", 55))
print(string.format("  Results: %d passed, %d failed", PASS, FAIL))
print(string.rep("=", 55))
if FAIL > 0 then os.exit(1) end
