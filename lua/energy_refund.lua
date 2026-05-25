-- energy_refund.lua
-- Refund energy when session expires without completion.
-- KEYS[1] = energy:balance:{b}:{u}
-- KEYS[2] = energy:reservation:{b}:{u}:{sid}
-- KEYS[3] = session:{sid}
-- KEYS[4] = active_session:{b}:{u}
-- ARGV[1] = session_id
-- Returns: {new_balance, refunded_amount} or error string

local session_id = ARGV[1]

-- 1. Check session exists
local status = redis.call('HGET', KEYS[3], 'status')
if not status then
    return redis.error_reply('SESSION_NOT_FOUND')
end

-- 2. If session already COMPLETED, do NOT refund (energy was legitimately consumed)
if status == 'COMPLETED' then
    return redis.error_reply('SESSION_ALREADY_COMPLETED')
end

-- 3. Get reservation amount
local reserved = tonumber(redis.call('GET', KEYS[2])) or 0
local balance = tonumber(redis.call('GET', KEYS[1])) or 0

-- 4. Refund if reservation exists
if reserved > 0 then
    balance = balance + reserved
    redis.call('SET', KEYS[1], balance)
    redis.call('DEL', KEYS[2])
end

-- 5. Mark session as EXPIRED
redis.call('HSET', KEYS[3], 'status', 'EXPIRED')

-- 6. Delete active_session lock
redis.call('DEL', KEYS[4])

return {balance, reserved}
