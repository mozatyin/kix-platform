-- energy_reserve.lua
-- Reserve energy for a game session (Rule 6: reservation pattern).
-- KEYS[1] = energy:balance:{b}:{u}
-- KEYS[2] = energy:regen_at:{b}:{u}
-- KEYS[3] = energy:reservation:{b}:{u}:{sid}
-- KEYS[4] = active_session:{b}:{u}
-- KEYS[5] = session:{sid}
-- ARGV[1]  = now (unix timestamp)
-- ARGV[2]  = cost (already ceiled per Rule 19: ceil(base * modifier))
-- ARGV[3]  = session_id
-- ARGV[4]  = user_id
-- ARGV[5]  = brand_id
-- ARGV[6]  = game_id
-- ARGV[7]  = season_id
-- ARGV[8]  = session_ttl (default 120)
-- ARGV[9]  = regen_interval (default 1500)
-- ARGV[10] = regen_cap (default 100)
-- Returns: {balance, cost} on success, or error string

local now = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local session_id = ARGV[3]
local user_id = ARGV[4]
local brand_id = ARGV[5]
local game_id = ARGV[6]
local season_id = ARGV[7]
local session_ttl = tonumber(ARGV[8])
local interval = tonumber(ARGV[9])
local cap = tonumber(ARGV[10])

-- 1. Check for active session (one session per user at a time)
local existing = redis.call('GET', KEYS[4])
if existing then
    return redis.error_reply('SESSION_DUPLICATE')
end

-- 2. Inline regen logic (same as energy_regen.lua)
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

-- 3. Check sufficient energy
if balance < cost then
    return redis.error_reply('INSUFFICIENT_ENERGY')
end

-- 4. Deduct energy
balance = balance - cost
redis.call('SET', KEYS[1], balance)

-- 5. Create reservation with TTL
redis.call('SET', KEYS[3], cost, 'EX', session_ttl)

-- 6. Set active session lock with TTL
redis.call('SET', KEYS[4], session_id, 'EX', session_ttl)

-- 7. Create session HASH with TTL=86400 (24h)
redis.call('HSET', KEYS[5],
    'user_id', user_id,
    'brand_id', brand_id,
    'game_id', game_id,
    'season_id', season_id,
    'status', 'CREATED',
    'score', 0,
    'cost', cost,
    'created_at', now)
redis.call('EXPIRE', KEYS[5], 86400)

-- 8. Return new balance and cost
return {balance, cost}
