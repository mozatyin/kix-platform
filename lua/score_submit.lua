-- score_submit.lua
-- Submit score and update leaderboard.
-- Rule 5: composite_score = score + (1 - ts/MAX_TS) for tie-breaking
-- Rule 17: score_submit must be followed by energy_confirm
-- KEYS[1] = session:{sid}
-- KEYS[2] = leaderboard:{b}:{g}:{season}
-- ARGV[1] = score
-- ARGV[2] = now (unix timestamp)
-- ARGV[3] = user_id
-- ARGV[4] = max_ts (2^40 = 1099511627776)
-- Returns: {score, rank} or error string

local score = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local user_id = ARGV[3]
local max_ts = tonumber(ARGV[4])

-- 1. Check session exists
local status = redis.call('HGET', KEYS[1], 'status')
if not status then
    return redis.error_reply('SESSION_NOT_FOUND')
end

-- 2. Idempotent: if already COMPLETED, return cached score and current rank
if status == 'COMPLETED' then
    local cached_score = tonumber(redis.call('HGET', KEYS[1], 'score')) or 0
    local rank = redis.call('ZREVRANK', KEYS[2], user_id)
    if rank then
        rank = rank + 1  -- 0-indexed to 1-indexed
    else
        rank = 0
    end
    return {cached_score, rank}
end

-- 3. Only CREATED sessions can submit scores
if status ~= 'CREATED' then
    return redis.error_reply('INVALID_SESSION_STATE')
end

-- 4. Validate score range
if score < 0 or score > 100000 then
    return redis.error_reply('INVALID_SCORE')
end

-- 5. Check minimum game duration (anti-cheat: at least 15 seconds)
local created_at = tonumber(redis.call('HGET', KEYS[1], 'created_at')) or now
if (now - created_at) < 15 then
    return redis.error_reply('GAME_TOO_SHORT')
end

-- 6. Update session with score and mark COMPLETED
redis.call('HSET', KEYS[1], 'score', score, 'status', 'COMPLETED', 'completed_at', now)

-- 7. Rule 5: composite_score = score + (1 - now/MAX_TS)
-- This ensures that among equal scores, the earlier submission ranks higher
local composite_score = score + (1 - now / max_ts)
redis.call('ZADD', KEYS[2], composite_score, user_id)

-- 8. Get rank (1-indexed)
local rank = redis.call('ZREVRANK', KEYS[2], user_id)
if rank then
    rank = rank + 1
else
    rank = 0
end

return {score, rank}
