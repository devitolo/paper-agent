"""Private, bounded MiniLM inference only. No application state or provider access."""
from __future__ import annotations
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import math
from pathlib import Path
import socket
import signal
import threading
import time
from .curator_interest import abstract_status
from .minilm_eval import MiniLMScorer, MODEL_DIR
from .minilm_http import API_VERSION, IDENTITY, MAX_BYTES, MAX_PAIRS


def validate_pairs(payload, *, raw_pairs=False):
    if not isinstance(payload,dict) or set(payload) != {'api_version','pairs'} or type(payload['api_version']) is not int or payload['api_version'] != API_VERSION:
        raise ValueError('invalid envelope')
    pairs = payload['pairs']
    if not isinstance(pairs,list) or not 1 <= len(pairs) <= MAX_PAIRS:
        raise ValueError('invalid batch')
    result=[]
    for item in pairs:
        if not isinstance(item,dict) or set(item) != {'title','abstract','interest'}:
            raise ValueError('invalid pair')
        if any(not isinstance(v,str) for v in item.values()):
            raise ValueError('strings required')
        if raw_pairs:
            if not all(item[k].strip() for k in ('title','abstract','interest')):
                raise ValueError('nonempty raw pair required')
            result.append((item['title'],item['abstract'],item['interest']))
            continue
        title=' '.join(item['title'].split())
        status,abstract=abstract_status(item['abstract'])
        if not title or status != 'valid' or not item['interest'].strip():
            raise ValueError('valid title, abstract and interest required')
        # Interest text is deliberately not normalized: preserve embedded semantics.
        result.append((title,abstract,item['interest']))
    return result


def metadata(model_dir):
    files={}
    for name in ('tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.txt','config.json'):
        path=Path(model_dir)/name
        if path.is_file(): files[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return {'identity':IDENTITY,'api_version':API_VERSION,'tokenizer_files':files,
            'runtime':{name:importlib.metadata.version(name) for name in ('onnxruntime','transformers','numpy','tokenizers')},
            'max_pairs':MAX_PAIRS,'max_request_bytes':MAX_BYTES,'inference_concurrency':1}


class InferenceServer(ThreadingHTTPServer):
    daemon_threads=True
    request_queue_size=4

    def __init__(self,address,scorer,details,budget=10):
        self.scorer=scorer
        self.details=details
        self.budget=budget
        self.inference=threading.Lock()
        self.connections=threading.BoundedSemaphore(4)
        super().__init__(address,Handler)

    def get_request(self):
        request,address=super().get_request()
        request.settimeout(3)
        return request,address

    def process_request(self,request,address):
        if not self.connections.acquire(blocking=False):
            try: request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError: pass
            self.shutdown_request(request)
            return
        try: super().process_request(request,address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self,request,address):
        try: super().process_request_thread(request,address)
        finally: self.connections.release()

    def handle_error(self,request,address):
        # Do not emit request bodies, client content, or exception tracebacks.
        pass


class Handler(BaseHTTPRequestHandler):
    server_version='PaperMiniLM/1'
    sys_version=''

    def log_message(self,*args): pass

    def respond(self,status,payload):
        body=json.dumps(payload,allow_nan=False).encode()
        try:
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Connection','close')
            self.end_headers()
            self.wfile.write(body)
        except (OSError,socket.timeout): pass
        self.close_connection=True

    def do_GET(self):
        if self.path not in ('/ready','/version'):
            return self.respond(404,{'error':'not_found'})
        self.respond(200,{'ready':True, **self.server.details})

    def do_POST(self):
        if self.path not in ('/v1/score','/v1/score-raw'): return self.respond(404,{'error':'not_found'})
        lengths=self.headers.get_all('Content-Length',[])
        if (len(lengths)!=1 or self.headers.get('Transfer-Encoding') is not None
                or self.headers.get_content_type()!='application/json'):
            return self.respond(400,{'error':'invalid_framing'})
        try: length=int(lengths[0])
        except ValueError: return self.respond(400,{'error':'invalid_length'})
        if not 0 < length <= MAX_BYTES: return self.respond(413,{'error':'request_size'})
        try:
            raw=self.rfile.read(length)
            if len(raw)!=length: raise ValueError('short body')
            pairs=validate_pairs(json.loads(raw),raw_pairs=self.path=='/v1/score-raw')
        except (ValueError,TypeError,UnicodeError): return self.respond(422,{'error':'invalid_input'})
        except (OSError,socket.timeout): return self.respond(408,{'error':'read_timeout'})
        if not self.server.inference.acquire(blocking=False):
            return self.respond(503,{'error':'overloaded'})
        try:
            deadline=time.monotonic()+self.server.budget
            scores=[]
            for title,abstract,interest in pairs:
                if time.monotonic()>=deadline: return self.respond(504,{'error':'inference_budget'})
                score=float(self.server.scorer.score_pair(title,abstract,interest))
                if not math.isfinite(score): raise ValueError('nonfinite')
                scores.append(score)
            if time.monotonic()>=deadline: return self.respond(504,{'error':'inference_budget'})
            self.respond(200,{'api_version':API_VERSION,'identity':IDENTITY,'scores':scores})
        except Exception:
            self.respond(503,{'error':'inference_unavailable'})
        finally: self.server.inference.release()


def create_server(address,model_dir=MODEL_DIR):
    # Construct once before binding: loading failure means no ready listener.
    scorer=MiniLMScorer(model_dir)
    return InferenceServer(address,scorer,metadata(model_dir))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8091)
    args=parser.parse_args()
    with create_server((args.host,args.port)) as server:
        def stop(signum, frame):
            # shutdown must run outside the serve_forever thread to avoid deadlock.
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever()

if __name__=='__main__': main()
