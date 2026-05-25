-- energy_grant.lua
-- Grant energy from QR scan with idempotency + cooldown.
-- KEYS[1] = energy:balance:{b}:{u}
-- KEYS[2] = energy:regen_at:{b}:{u}
-- KEYS[3] = cooldown:{b}:{u}
-- KEYS[4] = grant_idempotency:{u}:{nonce}
-- ARGV[1] = now (unix timestamp)
-- ARGV[2] = grant_amount (default 25)
-- ARGV[3] = overcap (default 130, QR can exceed normal cap)
-- ARGV[4] = cooldown_ttl (default 14400 = 4h)
-- ARGV[5] = idempotency_ttl (default 1800 = 30min)
-- ARGV[6] = regen_interval (default 1500)
-- ARGV[7] = regen_cap (default 100)
-- Returns: {new_balance, actual_granted} or error string

local now = tonumber(ARGV[1])
local grant_amount = tonumber(ARGV[2])
local overcap = tonumber(ARGV[3])
local cooldown_ttl = tonumber(ARGV[4])
local idempotency_ttl = tonumber(ARGV[5])
local interval = tonumber(ARGV[6])
local cap = tonumber(ARGV[7])

-- 1. Check idempotency (same QR nonce already used)
if redis.call('GET', KEYS[4]) then
    return redis.error_reply('ALREADY_GRANTED')
end

-- 2. Check cooldown (user scanned too recently)
local cd_ttl = redis.call('TTL', KEYS[3])
if cd_ttl > 0 then
    return redis.error_reply('COOLDOWN_ACTIVE:' .. cd_ttl)
end

-- 3. Run regen inline first
local balance = tonumber(redis.call('GET', KEYS[1])) or 0
local last_regen = tonumber(redis.call('GET', KEYS[2])) or now

if balance < cap then
    local elapsed = now - last_regen
    if elapsed > 0 then
        local earned = math.floor(elapsed / interval)
        if earned > 0 then
            balance = math.min(balance + earned, cap)
            -- Rule 14: preserve remainder
            last_regen = last_regen + earned * interval
            redis.call('SET', KEYS[2], last_regen)
        end
    end
end

-- 4. Grant energy up to overcap (QR can exceed normal 100 cap, up to 130)
local new_balance = math.min(balance + grant_amount, overcap)
local actual_granted = new_balance - balance

-- 5. SET new balance
redis.call('SET', KEYS[1], new_balance)

-- 6. SET cooldown with TTL
redis.call('SET', KEYS[3], '1', 'EX', cooldown_ttl)

-- 7. SET idempotency with TTL
redis.call('SET', KEYS[4], '1', 'EX', idempotency_ttl)

return {new_balance, actual_granted}
