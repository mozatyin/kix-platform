-- rate_limit.lua
-- Sliding window rate limiter using sorted set.
-- KEYS[1] = rate:{endpoint}:{b}:{u}
-- ARGV[1] = now (unix timestamp, can be float for sub-second precision)
-- ARGV[2] = window_seconds
-- ARGV[3] = max_requests
-- Returns: {allowed (1=yes, 0=no), current_count}

local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_requests = tonumber(ARGV[3])

-- 1. Remove entries older than the window
local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)

-- 2. Count current entries in the window
local count = redis.call('ZCARD', KEYS[1])

-- 3. Check if over limit
if count >= max_requests then
    return {0, count}
end

-- 4. Add current request
-- Use now as score; for member uniqueness, append a random suffix
-- to handle multiple requests in the same millisecond
local member = now .. ':' .. math.random(1000000)
redis.call('ZADD', KEYS[1], now, member)

-- 5. Set TTL on the key to auto-cleanup (window + 1s buffer)
redis.call('EXPIRE', KEYS[1], window + 1)

return {1, count + 1}
