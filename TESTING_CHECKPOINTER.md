# Testing PostgreSQL Checkpoint Feature Locally

This guide describes how to test the PostgreSQL checkpointer with optional S3 offloading in your local development environment.

## Prerequisites

**Local dev stack (started via `just start-local`):**
- PostgreSQL (`postgres-checkpointer` container on port 5403)
- Orchestrator, Agent Creator, Agent Runner services
- Optional: Localstack for local S3 emulation

## Test 1: Basic Checkpoint Persistence

**Goal:** Verify that conversation state is persisted across requests without S3.

### Steps

1. Start local environment (no S3):
   ```bash
   just start-local
   ```

2. Send a multi-turn conversation to the orchestrator:
   ```bash
   curl -X POST http://localhost:8000/api/chat \
     -H "Authorization: Bearer <your-jwt>" \
     -H "Content-Type: application/json" \
     -d '{
       "conversation_id": "test-conv-1",
       "messages": [
         {"role": "user", "content": "What is 2+2?"}
       ]
     }'
   ```

3. Verify checkpoint was saved to PostgreSQL:
   ```bash
   docker exec nannos-local-postgres-checkpointer psql -U postgres -d checkpointer -c \
     "SELECT thread_id, COUNT(*) as checkpoint_count FROM checkpoints GROUP BY thread_id;"
   ```
   Expected: Row with `thread_id = test-conv-1::orchestrator` and `checkpoint_count > 0`

4. Send a follow-up message using same `conversation_id`:
   ```bash
   curl -X POST http://localhost:8000/api/chat \
     -H "Authorization: Bearer <your-jwt>" \
     -H "Content-Type: application/json" \
     -d '{
       "conversation_id": "test-conv-1",
       "messages": [
         {"role": "user", "content": "And what is 3+3?"}
       ]
     }'
   ```

5. Verify the agent has access to previous context (should reference "2+2" answer in response).

### Expected Result

- ✅ Checkpoint created with correct `thread_id`
- ✅ Follow-up message restores conversation history
- ✅ No S3 objects created (all data in PostgreSQL)

---

## Test 2: S3 Offloading with Threshold

**Goal:** Verify large checkpoints are offloaded to S3 when threshold is exceeded.

### Setup

1. Start Localstack S3 (optional; or use real AWS):
   ```bash
   docker run -d --name localstack -p 4566:4566 localstack/localstack:latest
   ```

2. Create test S3 bucket:
   ```bash
   aws s3 mb s3://test-checkpoints \
     --endpoint-url http://localhost:4566 \
     --region us-east-1
   ```

3. Update local environment:
   ```bash
   # .env or environment variables
   export CHECKPOINT_S3_BUCKET_NAME=test-checkpoints
   export CHECKPOINT_S3_THRESHOLD_MB=0.5  # 500 KB threshold
   export AWS_S3_ENDPOINT_URL=http://localhost:4566  # For Localstack
   ```

4. Restart services:
   ```bash
   just stop-local
   just start-local
   ```

### Steps

1. Send a large multi-turn conversation (with large attachments/contexts):
   ```bash
   # Create a large file context (>500 KB)
   python3 << 'EOF'
   import json
   large_context = "x" * 600_000  # 600 KB
   payload = {
       "conversation_id": "test-conv-large",
       "messages": [
           {"role": "user", "content": f"Analyze this data: {large_context}"}
       ]
   }
   print(json.dumps(payload))
   EOF
   
   # Send via curl
   curl -X POST http://localhost:8000/api/chat \
     -H "Authorization: Bearer <your-jwt>" \
     -H "Content-Type: application/json" \
     -d @large_payload.json
   ```

2. Check PostgreSQL for S3 references:
   ```bash
   docker exec nannos-local-postgres-checkpointer psql -U postgres -d checkpointer -c \
     "SELECT thread_id, data FROM checkpoint_blobs WHERE thread_id LIKE 'test-conv-large%' LIMIT 1 \G"
   ```
   
   Expected: JSON with `{"s3_key": "checkpoints/<uuid>", "original_type": "..."}`

3. Verify S3 object was created:
   ```bash
   # For Localstack:
   aws s3 ls s3://test-checkpoints \
     --endpoint-url http://localhost:4566 \
     --region us-east-1
   
   # Or use boto3:
   python3 << 'EOF'
   import boto3
   s3 = boto3.client('s3', endpoint_url='http://localhost:4566')
   response = s3.list_objects_v2(Bucket='test-checkpoints')
   for obj in response.get('Contents', []):
       print(f"Key: {obj['Key']}, Size: {obj['Size']} bytes")
   EOF
   ```

### Expected Result

- ✅ Large checkpoint triggers S3 offload (blob in DB is just reference)
- ✅ `checkpoint_blobs` table contains only JSON reference, not full data
- ✅ S3 bucket has `checkpoints/<uuid>` objects with actual blob data
- ✅ Follow-up messages correctly fetch and deserialize from S3

---

## Test 3: Checkpoint Thread ID Isolation

**Goal:** Verify thread-id isolation prevents checkpoint collision between concurrent conversations.

### Steps

1. Send multiple concurrent conversations with different IDs:
   ```bash
   for i in {1..5}; do
     curl -X POST http://localhost:8000/api/chat \
       -H "Authorization: Bearer <your-jwt>" \
       -H "Content-Type: application/json" \
       -d "{\"conversation_id\": \"conv-$i\", \"messages\": [{\"role\": \"user\", \"content\": \"Request $i\"}]}" &
   done
   wait
   ```

2. Query checkpoints by thread_id:
   ```bash
   docker exec nannos-local-postgres-checkpointer psql -U postgres -d checkpointer -c \
     "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id;"
   ```

   Expected: 5 rows with distinct `thread_id = conv-<N>::orchestrator`

3. Verify each checkpoint contains only its own messages:
   ```bash
   docker exec nannos-local-postgres-checkpointer psql -U postgres -d checkpointer -c \
     "SELECT thread_id, data FROM checkpoints WHERE thread_id = 'conv-1::orchestrator' ORDER BY checkpoint_ns;"
   ```

### Expected Result

- ✅ Each conversation has isolated checkpoints
- ✅ No message leakage between different `conversation_id` threads
- ✅ Concurrent requests don't corrupt checkpoint state

---

## Test 4: Run Unit Tests

**Goal:** Verify S3OffloadingSerde unit tests pass.

### Steps

```bash
cd packages/ringier-a2a-sdk

# Run S3 offloading tests (mocked)
pytest tests/test_postgres_checkpointer_s3_offload.py -v

# With real S3 (optional):
WITH_REAL_S3=1 CHECKPOINT_S3_BUCKET_NAME=test-checkpoints \
  pytest tests/test_postgres_checkpointer_s3_offload.py::TestS3OffloadingRealS3 -v
```

### Expected Result

- ✅ All S3OffloadingSerde tests pass
- ✅ Threshold-based offloading logic works correctly
- ✅ S3 reference JSON has correct structure

---

## Test 5: Monitor PostgreSQL Schema

**Goal:** Inspect the checkpoint schema to understand storage layout.

### Steps

1. Connect to checkpoint database:
   ```bash
   docker exec -it nannos-local-postgres-checkpointer psql -U postgres -d checkpointer
   ```

2. Inspect tables:
   ```sql
   -- List all tables
   \dt
   
   -- Check checkpoints table schema
   \d checkpoints
   
   -- Check checkpoint_blobs table schema
   \d checkpoint_blobs
   
   -- Count checkpoints by thread_id
   SELECT thread_id, COUNT(*) as count FROM checkpoints GROUP BY thread_id;
   
   -- Check blob sizes
   SELECT thread_id, pg_size_pretty(pg_total_relation_size('checkpoint_blobs'::regclass)) as size;
   
   -- List recent blobs (S3 references show up in type = 's3ref')
   SELECT type, COUNT(*) FROM checkpoint_blobs GROUP BY type;
   ```

### Expected Result

- ✅ Tables: `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`
- ✅ Each blob has `type` tag (either JSON+, dict, list, etc., or 's3ref')
- ✅ S3-offloaded blobs show `type = 's3ref'` with compact JSON reference

---

## Troubleshooting

### "Checkpointer connection refused"

**Symptom:** Services log `connection refused` to PostgreSQL

**Fix:**
```bash
# Check if container is running
docker ps | grep postgres-checkpointer

# Check logs
docker logs nannos-local-postgres-checkpointer

# Restart
docker compose -f scripts/local-dev/docker-compose.yml restart postgres-checkpointer
sleep 5
```

### "S3 bucket does not exist"

**Symptom:** S3 offloading fails with 404 / NoSuchBucket

**Fix:**
```bash
# Create bucket
aws s3 mb s3://test-checkpoints \
  --endpoint-url http://localhost:4566 \
  --region us-east-1

# Or disable S3 offloading:
unset CHECKPOINT_S3_BUCKET_NAME
```

### "S3 offloading not triggered"

**Symptom:** Blobs stored in PostgreSQL even though `CHECKPOINT_S3_BUCKET_NAME` is set

**Debug:**
```bash
# Check threshold is set correctly (should be small for testing)
echo $CHECKPOINT_S3_THRESHOLD_MB  # Should be 0.5 or smaller

# Check S3OffloadingSerde is initialized
# Add debug log in postgres_checkpointer_mixin.py _build_serde()

# Verify blob size exceeds threshold
# Checkpoint payloads default to ~50 KB; set threshold to 0.01 MB (10 KB) for testing
export CHECKPOINT_S3_THRESHOLD_MB=0.01
```

### "Conversation state is not persisted"

**Symptom:** Follow-up messages don't have access to previous context

**Debug:**
```bash
# Verify checkpointer is actually being used (not MemorySaver)
docker logs orchestrator-agent | grep "PostgreSQL checkpointer ready"

# Check CHECKPOINT_POSTGRES_HOST is set
docker exec orchestrator-agent env | grep CHECKPOINT_POSTGRES

# Verify checkpoint was saved
docker exec nannos-local-postgres-checkpointer psql -U postgres -d checkpointer -c \
  "SELECT COUNT(*) FROM checkpoints;"
```

---

## Performance Baseline (Local)

Expected checkpoint latencies with local PostgreSQL + Localstack S3:

| Scenario | Latency | Notes |
|----------|---------|-------|
| Small checkpoint (<100 KB), DB only | ~50 ms | No S3 overhead |
| Large checkpoint (500 KB), S3 offload | ~200 ms | Network I/O to Localstack |
| Fetch checkpoint (DB + S3) | ~150 ms | Parallel fetch from both |

In production (AWS RDS + S3), expect similar or slightly higher due to network latency.

---

## References

- `packages/ringier-a2a-sdk/ringier_a2a_sdk/agent/postgres_checkpointer_mixin.py` — S3OffloadingSerde implementation
- `scripts/local-dev/docker-compose.yml` — PostgreSQL checkpointer container
- `scripts/start-local.sh` — Local environment setup (CHECKPOINT_* vars)
- Test file: `packages/ringier-a2a-sdk/tests/test_postgres_checkpointer_s3_offload.py`
