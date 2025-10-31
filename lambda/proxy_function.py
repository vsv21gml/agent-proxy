import json
import boto3
import time
import os
from datetime import datetime, timedelta
import hashlib
from rediscluster import RedisCluster
import redis
import socket
import urllib3
import errno

def get_redis_client():
    """Redis 클라이언트 연결 (연결 풀 사용)"""
    redis_host = os.environ.get('REDIS_HOST')
    redis_port = int(os.environ.get('REDIS_PORT', 6379))
    
    redis_client = redis.Redis(
        host=redis_host,
        port=redis_port,
        decode_responses=True,
        socket_connect_timeout=10,
        socket_timeout=10,
        retry_on_timeout=True,
        max_connections=5
    )

    # 연결 테스트
    redis_client.ping()
    print("redis connection established")

    return redis_client

# Bedrock Agent 클라이언트
bedrock_agent = boto3.client('bedrock-agent-runtime', region_name='us-east-1')

def lambda_handler(event, context):
    print(event)
    try:
        # API Key 또는 사용자 식별
        api_key = get_api_key(event)
        if not api_key:
            return error_response(401, "API Key required")
        
        # Redis 연결
        redis_conn = get_redis_client()
        
        # 유량 제어 검사
        rate_limit_result = check_rate_limit(redis_conn, api_key)
        if not rate_limit_result['allowed']:
            return error_response(429, "Rate limit exceeded", {
                'X-Rate-Limit-Remaining': '0',
                'X-Rate-Limit-Reset': str(rate_limit_result['reset_time'])
            })
        print('rate_limit check success')
        
        # 사용량 기록
        log_usage(redis_conn, api_key)
        print('log_usage success')
        
        # Bedrock Connection Test
        # conn_test = test_network_connectivity(event)
        # if not conn_test:
        #     return error_response(400, "Connection Fail")

        # Bedrock Agent 호출
        response = invoke_bedrock_agent(event)
        print('invoke_bedrock_agent success')
        print(response)
        
        # 응답 후 추가 메트릭 기록
        # log_response_metrics(redis_conn, api_key, response)
        # print('log_response_metrics success')
        
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'X-Rate-Limit-Remaining': str(rate_limit_result['remaining']),
                'X-Rate-Limit-Reset': str(rate_limit_result['reset_time'])
            },
            'body': json.dumps(response)
        }
        
    except redis.RedisError as e:
        print(f"Redis error: {str(e)}")
        return error_response(503, "Service temporarily unavailable")
    except Exception as e:
        print(f"Error: {str(e)}")
        return error_response(500, "Internal server error")

def get_api_key(event):
    """API Key 추출"""
    headers = event.get('headers', {})
    return headers.get('x-api-key') or headers.get('Authorization', '').replace('Bearer ', '')

def check_rate_limit(redis_conn, api_key):
    """
    Redis를 사용한 유량 제어 (Sliding Window Counter)
    """
    print(f"check_rate_limit key={api_key}")
    current_time = int(time.time())
    window_size = 60  # 1분 윈도우
    max_requests = get_rate_limit_for_user(redis_conn, api_key)  # 사용자별 제한
    
    # Redis key
    key = f"rate_limit:{{{api_key}}}:{current_time // window_size}"
    
    try:
        with redis_conn.pipeline() as pipe:
            # 트랜잭션 시작
            pipe.multi()
            
            # 현재 카운트 증가
            pipe.incr(key)
            
            # TTL 설정 (윈도우 크기의 2배)
            pipe.expire(key, window_size * 2)
            
            # 실행
            results = pipe.execute()
            current_count = results[0]
            
            # 이전 윈도우도 확인 (더 정확한 sliding window)
            prev_key = f"rate_limit:{api_key}:{(current_time // window_size) - 1}"
            prev_count = redis_conn.get(prev_key) or 0
            prev_count = int(prev_count)
            
            # 현재 시간이 윈도우에서 차지하는 비율
            window_start = (current_time // window_size) * window_size
            elapsed_ratio = (current_time - window_start) / window_size
            
            # 가중 평균으로 요청 수 계산
            estimated_count = int(prev_count * (1 - elapsed_ratio) + current_count)
            
            if estimated_count > max_requests:
                # 초과한 경우 현재 요청 취소
                redis_conn.decr(key)
                return {
                    'allowed': False,
                    'remaining': 0,
                    'reset_time': window_start + window_size
                }
            
            return {
                'allowed': True,
                'remaining': max(0, max_requests - estimated_count),
                'reset_time': window_start + window_size
            }
            
    except Exception as e:
        print(f"Rate limit check error: {str(e)}")
        # Redis 오류시 기본적으로 허용 (fallback)
        return {'allowed': True, 'remaining': max_requests, 'reset_time': current_time + window_size}

def get_rate_limit_for_user(redis_conn, api_key):
    # 캐시에서 사용자 등급 조회
    user_tier_key = f"user_tier:{api_key}"
    user_tier = redis_conn.get(user_tier_key) or 'free'
    
    # 등급별 제한
    rate_limits = {
        'free': 5,      # 분당 10개
        'premium': 60,   # 분당 60개
        'enterprise': 300 # 분당 300개
    }
    
    return rate_limits.get(user_tier, 10)

def log_usage(redis_conn, api_key):
    """사용량 기록 (다양한 시간 단위)"""
    current_time = int(time.time())
    current_date = datetime.utcnow().strftime('%Y-%m-%d')
    current_hour = datetime.utcnow().strftime('%Y-%m-%d:%H')
    
    with redis_conn.pipeline() as pipe:
        pipe.multi()
        
        # 해시 태그 {api_key}를 사용하여 모든 키가 같은 슬롯에 저장되도록 함
        minute_key = f"usage:minute:{{{api_key}}}:{current_time // 60}"
        pipe.incr(minute_key)
        pipe.expire(minute_key, 3600)
        
        hour_key = f"usage:hour:{{{api_key}}}:{current_hour}"
        pipe.incr(hour_key)
        pipe.expire(hour_key, 86400 * 7)
        
        daily_key = f"usage:daily:{{{api_key}}}:{current_date}"
        pipe.incr(daily_key)
        pipe.expire(daily_key, 86400 * 30)
        
        total_key = f"usage:total:{{{api_key}}}"
        pipe.incr(total_key)
        
        pipe.execute()

def log_response_metrics(redis_conn, api_key, response):
    """응답 메트릭 기록"""
    current_time = int(time.time())
    
    # 응답 크기 계산
    response_size = len(json.dumps(response).encode('utf-8'))
    
    with redis_conn.pipeline() as pipe:
        pipe.multi()
        
        # 응답 크기 누적
        size_key = f"metrics:response_size:{api_key}"
        pipe.incrby(size_key, response_size)
        pipe.expire(size_key, 86400)  # 1일 보관
        
        # 성공 카운트
        success_key = f"metrics:success:{api_key}:{current_time // 3600}"
        pipe.incr(success_key)
        pipe.expire(success_key, 86400 * 7)  # 7일 보관
        
        pipe.execute()

def invoke_bedrock_agent(event):
    """Bedrock Agent 호출"""
    try:
        body = event.get('body', {})
        
        agent_id = body.get('agentId')
        agent_alias_id = body.get('agentAliasId', 'TSTALIASID')
        session_id = body.get('sessionId', f"session-{int(time.time())}")
        input_text = body.get('inputText', '')
        
        if not agent_id or not input_text:
            raise ValueError("agentId and inputText are required")

        print(f"agent_id={agent_id}")
        print(f"agent_alias_id={agent_alias_id}")
        print(f"sessionId={session_id}")
        print(f"inputText={input_text}")
        
        response = bedrock_agent.invoke_agent(
            agentId=agent_id,
            agentAliasId=agent_alias_id,
            sessionId=session_id,
            inputText=input_text
        )
        
        # 스트리밍 응답 처리
        result = ""
        for event in response.get('completion', []):
            print(event)
            if 'chunk' in event:
                chunk = event['chunk']
                print(f"chunk={chunk}")
                if 'bytes' in chunk:
                    result += chunk['bytes'].decode('utf-8')
        
        return {
            'sessionId': session_id,
            'response': result,
            'agentId': agent_id,
            'timestamp': datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        raise Exception(f"Bedrock Agent invocation failed: {str(e)}")

def error_response(status_code, message, additional_headers=None):
    """에러 응답"""
    headers = {'Content-Type': 'application/json'}
    if additional_headers:
        headers.update(additional_headers)
    
    return {
        'statusCode': status_code,
        'headers': headers,
        'body': json.dumps({'error': message})
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
    
