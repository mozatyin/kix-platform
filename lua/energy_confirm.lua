-- energy_confirm.lua
-- Confirm energy reservation (game completed successfully).
-- Called after score_submit (Rule 17: score_submit must chain energy_confirm).
-- KEYS[1] = energy:reservation:{b}:{u}:{sid}
-- KEYS[2] = session:{sid}
-- KEYS[3] = active_session:{b}:{u}
-- ARGV[1] = session_id
-- Returns: "OK" or error string

local session_id = ARGV[1]

-- 1. Check session exists
local status = redis.call('HGET', KEYS[2], 'status')
if not status then
    return redis.error_reply('SESSION_NOT_FOUND')
end

-- 2. Idempotent: if already COMPLETED, still clean up and return OK
if status == 'COMPLETED' then
    redis.call('DEL', KEYS[1])
    redis.call('DEL', KEYS[3])
    return 'OK'
end

-- 3. Only CREATED sessions can be confirmed
if status ~= 'CREATED' then
    return redis.error_reply('INVALID_SESSION_STATE')
end

-- 4. Delete reservation key (energy is consumed, no refund possible)
redis.call('DEL', KEYS[1])

-- 5. Update session status to COMPLETED
redis.call('HSET', KEYS[2], 'status', 'COMPLETED')

-- 6. Delete active_session lock (user can start new game)
redis.call('DEL', KEYS[3])

return 'OK'
