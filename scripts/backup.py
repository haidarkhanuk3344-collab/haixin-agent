"""Local snapshot/restore administration; never overwrites without a safety snapshot."""
import argparse,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from personal_agent.app import load_config,AppError
from personal_agent.backups import Backups
p=argparse.ArgumentParser();p.add_argument('action',choices=['create','restore']);p.add_argument('--config');p.add_argument('--path');a=p.parse_args()
try:
 b=Backups(load_config(a.config));print(b.create() if a.action=='create' else b.restore(a.path))
except AppError as e:print(e.message);raise SystemExit(1)
