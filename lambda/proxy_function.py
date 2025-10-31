import os
import json
import redis
import boto3
import time
import uuid

import socket
import errno

# 환경 변수에서 설정값 가져오기
REDIS_HOST = 'bedrock-proxy-redis-001.bedrock-proxy-redis.bop0j9.use1.cache.amazonaws.com'
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
RPM_LIMIT = int(os.environ.get('RPM_LIMIT', 100))
TPM_LIMIT = int(os.environ.get('TPM_LIMIT', 10000))

# Redis 및 Bedrock 클라이언트 초기화
redis_client = None
try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, ssl=True, decode_responses=True)
    redis_client.ping()
    print("redis connection established")
except redis.exceptions.ConnectionError as e:
    print(f"Redis 연결에 실패했습니다: {e}")
    redis_client = None

bedrock_agent_runtime_client = boto3.client('bedrock-agent-runtime')

def lambda_handler(event, context):
    conn_test = test_network_connectivity(event)
    if not conn_test:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Connection Error'})
        }

    if not redis_client:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Redis 클라이언트를 초기화할 수 없습니다.'})
        }

    body = event.get('body', {})
    api_key = event.get('headers', {}).get('x-api-key')
    prompt = body.get('inputText')
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
    print(f"current_minute=${current_minute} rpm_key=${rpm_key}")

    # 파이프라인을 사용하여 원자적 연산 수행
    try:
        with redis_client.pipeline() as pipeline:
            # 트랜잭션 시작
            pipeline.multi()

            pipeline.incr(rpm_key)
            pipeline.expire(rpm_key, 60)
            pipeline.get(rpm_key)
            
            pipeline.incrby(tpm_key, prompt_tokens)
            pipeline.expire(tpm_key, 60)
            pipeline.get(tpm_key)
            
            results = pipeline.execute()
            print(results)

            current_rpm = int(results[2])
            current_tpm = int(results[5])
            print(f"current_rpm={current_rpm}")

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

    except Exception as e:
        print(f"Rate limit check error: {str(e)}")
        # Redis 오류시 기본적으로 허용 (fallback)
        return {
            'statusCode': 429,
            'body': json.dumps({'error': f'Redis Error'})
        }

    # --- Bedrock Agent 호출 ---
    session_id = str(uuid.uuid4()) # 각 요청마다 고유 세션 ID 생성
    
    try:
        print(f"session_id=${session_id}")
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


def test_network_connectivity(event):
    """네트워크 연결 상태를 테스트하는 함수"""
    
    # 1. 더 자세한 네트워크 진단
    try:
        print("🔍 상세 네트워크 진단 시작...")
        
        # DNS 해상도
        hostname = 'httpbin.org'
        ip = socket.gethostbyname(hostname)
        print(f"✅ DNS: {hostname} -> {ip}")
        
        # 여러 포트로 연결 테스트
        ports = [80, 443, 8080]
        for port in ports:
            try:
                print(f"🔌 {ip}:{port} 연결 테스트...")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(15)  # 타임아웃 늘림
                
                start_time = time.time()
                result = sock.connect_ex((ip, port))
                elapsed = time.time() - start_time
                
                if result == 0:
                    print(f"✅ 포트 {port} 연결 성공 ({elapsed:.2f}초)")
                else:
                    error_msg = errno.errorcode.get(result, f"Unknown error {result}")
                    print(f"❌ 포트 {port} 연결 실패: {result} ({error_msg}) - {elapsed:.2f}초")
                
                sock.close()
            except Exception as e:
                print(f"❌ 포트 {port} 연결 예외: {e}")
        
        # 간단한 AWS 서비스 연결 테스트
        print("🔍 AWS 서비스 연결 테스트...")
        aws_endpoints = [
            ('s3.amazonaws.com', 443),
            ('ec2.amazonaws.com', 443)
        ]
        
        for endpoint, port in aws_endpoints:
            try:
                aws_ip = socket.gethostbyname(endpoint)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                result = sock.connect_ex((aws_ip, port))
                sock.close()
                
                if result == 0:
                    print(f"✅ AWS {endpoint} 연결 성공")
                else:
                    print(f"❌ AWS {endpoint} 연결 실패: {result}")
            except Exception as e:
                print(f"❌ AWS {endpoint} 연결 예외: {e}")
        
    except Exception as e:
        print(f"❌ 진단 실패: {e}")
        return False


    try:
        print("🔍 DNS 해상도 테스트...")
        ip = socket.gethostbyname('httpbin.org')
        print(f"✅ DNS 해상도 성공: httpbin.org -> {ip}")
    except Exception as e:
        print(f"❌ DNS 해상도 실패: {e}")
        return False
    
    # 2. 간단한 연결 테스트
    try:
        print("🔌 소켓 연결 테스트...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        result = sock.connect_ex((ip, 80))
        sock.close()
        
        if result == 0:
            print("✅ 소켓 연결 성공")
        else:
            print(f"❌ 소켓 연결 실패: {result}")
            return False
    except Exception as e:
        print(f"❌ 소켓 연결 오류: {e}")
        return False
    
    # 3. HTTP 요청 테스트
    try:
        print("🌐 HTTP 요청 테스트...")
        http = urllib3.PoolManager()
        response = http.request('GET', 'http://httpbin.org/ip', timeout=10)
        print(f"✅ HTTP 요청 성공: {response.status}")
        print(f"응답: {response.data.decode('utf-8')}")
    except Exception as e:
        print(f"❌ HTTP 요청 실패: {e}")
        return False
    
    return True
    
