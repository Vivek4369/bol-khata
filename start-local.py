"""Start the existing free local runtime and the app; downloads are never automatic."""
import json,os,secrets,subprocess,sys,time
from pathlib import Path
from urllib.request import Request,urlopen
root=Path(__file__).resolve().parent
runtime=root.parent.parent/'work'/'local-ai'
config={}
if (root/'.env').exists():
 for line in (root/'.env').read_text().splitlines():
  if '=' in line and not line.lstrip().startswith('#'):
   key,value=line.split('=',1);config[key.strip()]=value.strip()
for key,value in config.items():os.environ.setdefault(key,value)
if os.environ.get('LLAMA_URL'):
 url=os.environ['LLAMA_URL'];token=os.environ.get('LOCAL_AI_TOKEN','');process=None
 def ready():
  try:
   with urlopen(Request(url+'/health',headers={'Authorization':'Bearer '+token}),timeout=1) as r:return json.load(r).get('status')=='ok'
  except Exception:return False
 if not ready():
  manifest=runtime/'models/manifests/registry.ollama.ai/library/qwen3/4b-instruct'
  cli=runtime/'ollama/llama-server'
  if not cli.is_file() or not manifest.is_file():sys.exit('Local runtime is missing. See README for setup; no downloads were made.')
  digest=next(x['digest'].replace(':','-') for x in json.loads(manifest.read_text())['layers'] if x['mediaType']=='application/vnd.ollama.image.model')
  model=runtime/'models/blobs'/digest;key_file=runtime/'api-token'
  if not token:sys.exit('LOCAL_AI_TOKEN is missing from .env.')
  key_file.write_text(token);key_file.chmod(0o600)
  log=open(runtime/'cpu-model.log','a')
  process=subprocess.Popen([str(cli),'-m',str(model),'--device','none','--no-op-offload','-ngl','0','-c','4096','-np','1','--host','127.0.0.1','--port','11436','--api-key-file',str(key_file),'--no-webui','--offline'],stdout=log,stderr=log)
  for _ in range(30):
   if ready():break
   if process.poll() is not None:sys.exit('Local model failed to start. Check work/local-ai/cpu-model.log.')
   time.sleep(1)
  if not ready():process.terminate();sys.exit('Local model did not become ready.')
 try:subprocess.run([sys.executable,str(root/'server.py')],cwd=root)
 finally:
  if process:process.terminate();process.wait(timeout=10)
else:subprocess.run([sys.executable,str(root/'server.py')],cwd=root)
