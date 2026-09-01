"""Atomic Redis Lua contracts used by :mod:`redis_message_queue`."""

DISPATCH_SCRIPT = """
local receipt = KEYS[1]
local messages = KEYS[2]
local stream = KEYS[3]
local existing = redis.call('HGET', receipt, 'fingerprint')
if existing then
  if existing ~= ARGV[1] then return {'delivery_conflict'} end
  if redis.call('HGET', receipt, 'status') ~= 'returned' then
    return {'duplicate', redis.call('HGET', receipt, 'stream_id') or ''}
  end
end
local count = tonumber(ARGV[2])
for index = 1, count do
  local id = ARGV[2 + index]
  local raw = redis.call('HGET', messages, id)
  if not raw then return {'missing', id} end
  local value = cjson.decode(raw)
  if value['state'] ~= 'pending' then return {'state_conflict', id} end
  if raw ~= ARGV[2 + count + index] then return {'queue_changed', id} end
end
for index = 1, count do
  local id = ARGV[2 + index]
  local updated = ARGV[2 + (count * 2) + index]
  redis.call('HSET', messages, id, updated)
end
local envelope = ARGV[3 + (count * 3)]
local stream_id = redis.call('XADD', stream, '*', 'envelope', envelope)
redis.call('HSET', receipt, 'fingerprint', ARGV[1], 'status', 'dispatched', 'stream_id', stream_id, 'attempts', 0, 'envelope', envelope)
redis.call('PERSIST', receipt)
return {'created', stream_id}
"""

ACK_SCRIPT = """
local receipt = KEYS[1]
local stream = KEYS[2]
local messages = KEYS[3]
local order = KEYS[4]
local fingerprint = redis.call('HGET', receipt, 'fingerprint')
if not fingerprint then return {'missing_receipt'} end
if fingerprint ~= ARGV[1] then return {'delivery_conflict'} end
local status = redis.call('HGET', receipt, 'status')
if status == 'acknowledged' then return {'duplicate'} end
redis.pcall('XACK', stream, ARGV[2], ARGV[3])
redis.call('XDEL', stream, ARGV[3])
local count = tonumber(ARGV[4])
for index = 1, count do
  local id = ARGV[4 + index]
  redis.call('HDEL', messages, id)
  redis.call('ZREM', order, id)
end
redis.call('HSET', receipt, 'status', 'acknowledged', 'acknowledged_at', ARGV[5 + count])
redis.call('EXPIRE', receipt, tonumber(ARGV[6 + count]))
if redis.call('XLEN', stream) == 0 then redis.call('DEL', stream) end
return {'acknowledged'}
"""

DIRECT_DISPATCH_SCRIPT = """
local receipt = KEYS[1]
local stream = KEYS[2]
local existing = redis.call('HGET', receipt, 'fingerprint')
if existing then
  if existing ~= ARGV[1] then return {'delivery_conflict'} end
  return {'duplicate', redis.call('HGET', receipt, 'stream_id') or ''}
end
local stream_id = redis.call('XADD', stream, '*', 'envelope', ARGV[2])
redis.call('HSET', receipt, 'fingerprint', ARGV[1], 'status', 'dispatched', 'stream_id', stream_id, 'attempts', 0, 'envelope', ARGV[2])
redis.call('PERSIST', receipt)
return {'created', stream_id}
"""

DIRECT_ACK_SCRIPT = """
local receipt = KEYS[1]
local stream = KEYS[2]
local fingerprint = redis.call('HGET', receipt, 'fingerprint')
if not fingerprint then return {'missing_receipt'} end
if fingerprint ~= ARGV[1] then return {'delivery_conflict'} end
if redis.call('HGET', receipt, 'status') == 'acknowledged' then return {'duplicate'} end
redis.pcall('XACK', stream, ARGV[2], ARGV[3])
redis.call('XDEL', stream, ARGV[3])
redis.call('HSET', receipt, 'status', 'acknowledged', 'acknowledged_at', ARGV[4])
redis.call('EXPIRE', receipt, tonumber(ARGV[5]))
if redis.call('XLEN', stream) == 0 then redis.call('DEL', stream) end
return {'acknowledged'}
"""
