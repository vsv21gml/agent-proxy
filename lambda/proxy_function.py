import os
import json
import redis
import boto3
import time
import uuid

# 환경 변수에서 설정값 가져오기
REDIS_HOST = os.environ.get('REDIS_HOST')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
RPM_LIMIT = int(os.environ.get('RPM_LIMIT', 100))
TPM_LIMIT = int(os.environ.get('TPM_LIMIT', 10000))

# Redis 및 Bedrock 클라이언트 초기화
try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, ssl=True, decode_responses=True)
except redis.exceptions.ConnectionError as e:
    print(f"Redis 연결에 실패했습니다: {e}")
    redis_client = None

bedrock_agent_runtime_client = boto3.client('bedrock-agent-runtime')

def lambda_handler(event, context):
    if not redis_client:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Redis 클라이언트를 초기화할 수 없습니다.'})
        }

    body = event.get('body', {})
    api_key = event.get('headers', {}).get('x-api-key')
    prompt = body.get('prompt')
    agent_id = body.get('agentId')
    agent_alias_id = body.get('agentAliasId')

    if not all([api_key, prompt, agent_id, agent_alias_id]):
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'x-api-key, prompt, agentId, agentAliasId는 필수 필드입니다.'})
        }
        
    # 대략적인 토큰 수 계산 (단순히 4글자를 1토큰으로 가정)
    # 실제로는 Bedrock의 토크나이저를 사용하는 것이 더 정확합니다.
    prompt_tokens = len(prompt) // 4 

    # --- RPM 및 TPM 체크 ---
    current_minute = int(time.time() // 60)
    rpm_key = f"{api_key}:{current_minute}:rpm"
    tpm_key = f"{api_key}:{current_minute}:tpm"

    # 파이프라인을 사용하여 원자적 연산 수행
    pipeline = redis_client.pipeline()
    pipeline.incr(rpm_key)
    pipeline.expire(rpm_key, 60)
    pipeline.get(rpm_key)
    
    pipeline.incrby(tpm_key, prompt_tokens)
    pipeline.expire(tpm_key, 60)
    pipeline.get(tpm_key)
    
    results = pipeline.execute()

    current_rpm = int(results[2])
    current_tpm = int(results[5])

    if current_rpm > RPM_LIMIT:
        return {
            'statusCode': 429,
            'body': json.dumps({'error': f'분당 요청 한도({RPM_LIMIT} RPM)를 초과했습니다.'})
        }
    
    if current_tpm > TPM_LIMIT:
        # 이미 증가된 토큰 수를 롤백
        redis_client.decrby(tpm_key, prompt_tokens)
        return {
            'statusCode': 429,
            'body': json.dumps({'error': f'분당 토큰 한도({TPM_LIMIT} TPM)를 초과했습니다.'})
        }

    # --- Bedrock Agent 호출 ---
    session_id = str(uuid.uuid4()) # 각 요청마다 고유 세션 ID 생성
    
    try:
        response = bedrock_agent_runtime_client.invoke_agent(
            agentId=agent_id,
            agentAliasId=agent_alias_id,
            sessionId=session_id,
            inputText=prompt,
        )

        completion_text = ""
        for event in response.get('completion', []):
            if 'chunk' in event:
                chunk = event['chunk']
                completion_text += chunk['bytes'].decode('utf-8')

        return {
            'statusCode': 200,
            'body': json.dumps({'response': completion_text})
        }
    except Exception as e:
        print(f"Bedrock Agent 호출 중 오류 발생: {e}")
        # 오류 발생 시 TPM/RPM 롤백
        redis_client.decr(rpm_key)
        redis_client.decrby(tpm_key, prompt_tokens)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Bedrock Agent를 호출하는 동안 오류가 발생했습니다.'})
        }