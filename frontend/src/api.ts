export interface Affair {id:string;kind:string;title:string;body:string;due:number|null;remind:number|null;status:'active'|'done'|'cancelled';version:number;session:string}
export interface Reminder {id:string;session:string;body:string;created:number;mac_state:string;read_at:number|null}
export interface Job {mirrored_reminder?:boolean;id:string;kind:string;status:string;goal:string;error:string|null;texts:string[];created:string;session:string;has_artifact:boolean;cancel_requested:number;steps:{name:string;status:string}[]}
export interface Source {id:string;kind:string;locator:string;title?:string;current_version:number;status:string;updated:string}
export interface Watch {source_id:string;interval_seconds:number;enabled:number;last_checked:number|null;last_changed:number|null;next_due:number;last_error:string|null;active_run:string|null}
export interface Message {id:string;session:string;body:string|null;created:number;feedback:string|null;status:string;source_id:string|null;source_version:number|null;kind:string}
export interface Memory {id:string;content:string;kind:string;status:string;updated:string}
export interface Appointment {id:string;session:string;goal:string;mode:string;due:number;status:string;delivery_job:string|null}
export interface PersonalMemory {id:string;content:string;scope:"long"|"short";kind:string;status:string;certainty:string;subject:string;occurred:string;expires:number|null;updated:string;created:string;source_job:string;version:number}
export interface State {affairs?:Affair[];reminders?:Reminder[];notifications?:{status:string};balance?:{status:string;checked_at:number|null;balances:{currency:string;total_balance:string}[]};personal_memories?:PersonalMemory[];appointments?:Appointment[];jobs:Job[];sources:Source[];watches:Watch[];inbox:Message[];memories:Memory[];settings:{enabled:boolean;halt_reason:string|null;pause_until:number;daily_limit:number|null;quiet_start:string|null;quiet_end:string|null;min_interval_seconds:number};memory_settings:{enabled:Record<string,boolean>};worker:{state:string;message:string};time:string}
export interface Receipt {request_id:string;status:string;result:{job_id?:string;texts?:string[];error?:{code:string;message:string}}|null}
export function statusLabel(status:string):string{return ({waiting_balance:'等待余额恢复',queued:'排队中',running:'正在处理',succeeded:'已完成',failed:'未完成',cancelled:'已取消',outcome_unknown:'结果待核对',ready:'已入库'} as Record<string,string>)[status]??'状态待核对'}
export function parseState(data:unknown):State {
 if(!data||typeof data!=='object')throw Error('页面数据无法读取，请重新连接。');
 const d=data as Record<string,unknown>;
 for(const key of ['jobs','sources','watches','inbox','memories'])if(!Array.isArray(d[key]))throw Error('页面数据格式发生变化，请重新连接。');
 if(!d.settings||!d.worker||!d.memory_settings||typeof d.time!=='string')throw Error('页面状态不完整，请重新连接。');
 return data as State;
}
export async function request(path:string,body?:unknown):Promise<unknown>{
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),15000);
 try{
  const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{'X-Agent-UI':'1','Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal});
  const value:unknown=await response.json();
  if(!response.ok){const e=value as {error?:{message?:string}};throw Error(e.error?.message??'连接暂不可用，请重新打开页面。');}
  return value;
 }catch(error){if(error instanceof Error&&error.name==='AbortError')throw Error('连接超时，请先核对原请求；页面不会自动重发。');throw error;}finally{clearTimeout(timer);}
}
export async function act(action:Record<string,unknown>):Promise<Receipt>{
 if(sessionStorage.getItem('pending-request'))throw Error('还有一次提交需要核对，请先查看提交结果。');
 const id=crypto.randomUUID();sessionStorage.setItem('pending-request',id);
 const raw=await request('action',{...action,id});
 if(!raw||typeof raw!=='object'||!('status' in raw)||!['completed','failed','reserved','outcome_unknown'].includes(String(raw.status)))throw Error('提交响应无法核验，请先核对任务列表。');
 const response=raw as Receipt;
 if(response.status==='reserved'||response.status==='outcome_unknown')throw Error('提交结果待核对，请查看任务列表，不要重复发送。');
 sessionStorage.removeItem('pending-request');
 if(response.result?.error)throw Error(response.result.error.message);
 return response;
}
export async function recoverRequest():Promise<Receipt|null>{
 const id=sessionStorage.getItem('pending-request');if(!id)return null;
 const receipt=await request('request?id='+encodeURIComponent(id)) as Receipt;
 if(receipt.status==='completed'||receipt.status==='failed')sessionStorage.removeItem('pending-request');
 return receipt;
}
export async function download(id:string):Promise<void>{
 const r=await fetch('/api/artifact?id='+encodeURIComponent(id),{headers:{'X-Agent-UI':'1'}});
 if(!r.ok){const v=await r.json() as {error:{message:string}};throw Error(v.error.message);}
 const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='海辛-文档.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
