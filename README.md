# Rate Limiter

A small, in-memory, thread-safe rate limiter. It allows at most `limit`
requests per key in any trailing window of `window_seconds`.

```python
from rate_limiter import RateLimiter

limiter = RateLimiter(limit=200, window_seconds=60)

def call_database(pod_name):
    if limiter.allow(pod_name):
        return make_the_request()

    # This pod has already used its 200 requests for the last 60 seconds.
    return None
```

## What is counted

- Each key has its own independent list of requests. `allow("pod-a")` has no
  effect on `allow("pod-b")`.
- In this task the key is the **pod's name**.
- The provider's API key is **not** part of the limiter. The limiter never sees
  it; it only sees the label passed to `allow`.
- Three pods with a limit of 200 can therefore make up to 600 requests in
  total: 200 per pod.
- That is correct **only if the provider's cap is per pod**. If the provider
  has one global 200/min cap on a shared API key, this in-memory limiter cannot
  enforce it, because each process only knows its own counters. In that case
  you would need shared state (for example Redis), a split budget, or a
  separate provider credential per pod.

## Algorithm: sliding window log

For every key we keep the times of the requests that were allowed. On each
call we remove the times that are now too old, count what remains, and allow
the request if the count is below the limit.

1. Get the key's list (create it if this is the first call for that key).
2. Read the current time once.
3. Remove timestamps that are exactly `window_seconds` old or older.
4. If fewer than `limit` timestamps remain, add the current time and return
   `True`.
5. Otherwise return `False` and add nothing.

Example with `limit=2` and `window_seconds=10`:

| Time | List before | Action | Result |
| ---- | ----------- | ------ | ------ |
| 0 | `[]` | add 0 | `True` |
| 1 | `[0]` | add 1 | `True` |
| 2 | `[0, 1]` | list is full | `False` |
| 10 | `[0, 1]` | remove 0 (exactly 10s old), add 10 | `True` |

### Why this algorithm

- It matches the wording exactly: no more than `limit` allowed requests in any
  trailing window, not per calendar minute.
- A fixed window can allow 200 at 12:00:59 and another 200 at 12:01:00. The
  sliding window does not have that boundary problem.
- Memory is small at this scale: at most `limit` timestamps per active key.
- Each timestamp is added once and removed once, so the average cost per call
  is constant.

### Why not the alternatives

| Algorithm | Memory per key | Exact count in any window? | Main issue |
| --------- | -------------- | -------------------------- | ---------- |
| Fixed window | O(1) | No | up to 2× the limit across a boundary |
| Sliding window log (chosen) | O(limit) | Yes | more memory than a counter |
| Sliding window counter | O(1) | Approximate | estimate can be slightly wrong |
| Token bucket | O(1) | No | average rate plus bursts; can exceed N in a trailing window |
| Leaky bucket (queue) | O(queue) | Shapes output | delays requests; not a yes/no gate |

Token bucket is the better choice if the limit is very large, if constant
memory matters, or if the caller needs to wait for a token instead of getting
`False`. Fixed window is reasonable when simplicity matters more than exact
boundaries.

## Exact rules

- The window is half-open: `(now - window_seconds, now]`. A request exactly
  `window_seconds` old has expired.
- Denied requests are **not** recorded. A rejected call does not use up
  capacity.
- The clock is `time.monotonic` by default. It never jumps backwards. Tests
  pass a fake clock instead.
- The limiter records when a request was **allowed**, not when it finished. If
  an allowed request later fails, its slot is still used.
- Keys can be any hashable value. The limiter does not interpret them.

## Thread safety

- A short-lived lock protects the dictionary of key states and makes sure two
  threads cannot create two states for the same new key.
- Each key has its own lock. It protects that key's timestamp list and the
  check-and-record decision.
- The check and the record happen in the same critical section. Without the
  lock, two threads could both see 199 requests used, both allow, and reach
  201. The GIL does not make this sequence atomic.
- The clock is read while holding the key's lock, so timestamps are appended in
  order for that key.
- Different keys do not share a list or a lock. The dictionary lock is still
  taken briefly on every call, but it only covers lookup and creation.

This makes the limiter safe for threads in one process. It is **not**
distributed: two processes with the same key each keep their own count.

## Cost

- Time per call: amortised O(1). One call after a long idle period may remove
  many old timestamps at once.
- Memory: at most `limit` timestamps per active key.
- The dictionary of keys is never pruned. That is fine for a small set of pod
  names, but it would need eviction if keys were long-lived tenants.

## Running the tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests use a fake clock, so they do not sleep. They cover the window
boundary, partial expiry, denied calls, key isolation, invalid arguments, and
concurrent calls on one key and on many keys.

## Edge cases and what would change

- **Provider cap is global, not per pod:** per-process state cannot enforce a
  fleet-wide cap. Use shared state such as Redis, split the budget across pods,
  or give each pod its own provider credential.
- **Process restart:** counters reset, so a restarted process starts with a
  full allowance.
- **Many distinct keys:** the dictionary grows forever. Add idle-key eviction
  if keys are not a small fixed set.
- **Denied requests:** the caller decides whether to retry, queue, or drop.
  The limiter has no `retry_after`, so an aggressive caller may keep hammering,
  and provider 429s still need handling in the caller.
- **Allowed request then fails:** the slot stays used. Refunding it would be
  more forgiving but easier to abuse.
- **Burst at a window boundary:** many slots can expire together and the next
  burst is allowed immediately. Token bucket or GCRA smooths this.
