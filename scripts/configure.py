"""Connect an existing local OpenClaw installation without rewriting it."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from personal_agent.app import load_config,atomic_write
p=argparse.ArgumentParser()
for key in ('runtime','node','cli','state'):p.add_argument('--'+key,required=True)
a=p.parse_args();target=ROOT/'config.local.json'
if target.exists():raise SystemExit('config.local.json already exists; it was not changed.')
c=json.loads((ROOT/'config.example.json').read_text())
for key in ('runtime','node','cli','state'):c[key]=str(Path(getattr(a,key)).expanduser().resolve())
c['price_file']=str(ROOT/'config/prices.json')
# Validate the candidate before writing the destination.
import tempfile
with tempfile.TemporaryDirectory() as temp:
 probe=Path(temp)/'config.json';probe.write_text(json.dumps(c));load_config(probe)
atomic_write(target,json.dumps(c,ensure_ascii=False,indent=2))
print('Saved config.local.json. Original OpenClaw configuration and data were not changed.')
