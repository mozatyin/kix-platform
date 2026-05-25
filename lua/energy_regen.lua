-- energy_regen.lua
-- Lazy regeneration calculation.
-- KEYS[1] = energy:balance:{brand_id}:{user_id}
-- KEYS[2] = energy:regen_at:{brand_id}:{user_id}
-- ARGV[1] = now (unix timestamp)
-- ARGV[2] = regen_interval (default 1500 = 25min)
-- ARGV[3] = regen_cap (default 100)
-- Returns: {balance, last_regen_at}

local balance = tonumber(redis.call('GET', KEYS[1])) or 0
local last_regen = tonumber(redis.call('GET', KEYS[2])) or tonumber(ARGV[1])
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])

-- Rule 16: balance >= cap means regen is frozen (no increase, no decrease)
if balance >= cap then
    return {balance, last_regen}
end

local elapsed = now - last_regen
if elapsed < 0 then
    -- Clock skew protection: don't go backwards
    return {balance, last_regen}
end

local earned = math.floor(elapsed / interval)
if earned > 0 then
    balance = math.min(balance + earned, cap)
    -- Rule 14: preserve remainder! last_regen += earned * interval, NOT = now
    last_regen = last_regen + earned * interval
    redis.call('SET', KEYS[1], balance)
    redis.call('SET', KEYS[2], last_regen)
end

return {balance, last_regen}
