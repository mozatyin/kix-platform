-- streak_check.lua
-- Check and update user daily streak.
-- KEYS[1] = streak:{b}:{u}
-- ARGV[1] = today_sgt_date (string like "2026-05-23", SGT = UTC+8)
-- ARGV[2] = brand_id
-- ARGV[3] = user_id
-- Returns: {current_streak, longest_streak, today_completed (1=yes, 0=already done)}

local today = ARGV[1]

-- 1. Get current streak data
local current = tonumber(redis.call('HGET', KEYS[1], 'current')) or 0
local longest = tonumber(redis.call('HGET', KEYS[1], 'longest')) or 0
local last_date = redis.call('HGET', KEYS[1], 'last_completed_date')

-- 2. If already completed today, return as-is (idempotent)
if last_date == today then
    return {current, longest, 0}
end

-- 3. Determine if streak continues or resets
-- Parse dates to compute difference
-- Date format: YYYY-MM-DD
local function parse_date(d)
    local y, m, dd = d:match('(%d+)-(%d+)-(%d+)')
    return os.time({year=tonumber(y), month=tonumber(m), day=tonumber(dd), hour=0})
end

if last_date then
    local last_ts = parse_date(last_date)
    local today_ts = parse_date(today)
    local diff_days = math.floor((today_ts - last_ts) / 86400)

    if diff_days == 1 then
        -- Consecutive day: streak continues
        current = current + 1
    elseif diff_days <= 0 then
        -- Same day or time travel: no change (shouldn't reach here due to check above)
        return {current, longest, 0}
    else
        -- Gap > 1 day: streak broken, restart
        current = 1
    end
else
    -- First ever streak entry
    current = 1
end

-- 4. Update longest
if current > longest then
    longest = current
end

-- 5. Persist
redis.call('HSET', KEYS[1],
    'current', current,
    'longest', longest,
    'last_completed_date', today)

return {current, longest, 1}
