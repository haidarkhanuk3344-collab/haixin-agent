import {describe,it,expect,vi,afterEach} from 'vitest';
import {act,recoverRequest,parseState,statusLabel} from '../src/api';
describe('真实状态边界',()=>{
 it('未知状态不能显示成功',()=>{expect(statusLabel('mystery')).toBe('状态待核对');expect(statusLabel('outcome_unknown')).toBe('结果待核对')});
 it('坏接口数据不是空状态',()=>{expect(()=>parseState({})).toThrow();expect(()=>parseState(null)).toThrow();expect(()=>parseState({jobs:[]})).toThrow()});
 it('合法空数据才能进入空状态',()=>{const state={jobs:[],sources:[],watches:[],inbox:[],memories:[],settings:{enabled:true},worker:{state:'running'},memory_settings:{enabled:{}},time:'now'};expect(parseState(state).jobs).toEqual([])});
});

afterEach(()=>vi.unstubAllGlobals());
function storage(){const values=new Map<string,string>();vi.stubGlobal('sessionStorage',{getItem:(k:string)=>values.get(k)??null,setItem:(k:string,v:string)=>values.set(k,v),removeItem:(k:string)=>values.delete(k)});return values}
it('断网保留原请求编号，禁止盲目重复提交',async()=>{const values=storage();const network=vi.fn().mockRejectedValue(Error('断网'));vi.stubGlobal('fetch',network);await expect(act({action:'chat'})).rejects.toThrow();expect(values.has('pending-request')).toBe(true);await expect(act({action:'chat'})).rejects.toThrow('核对');expect(network).toHaveBeenCalledTimes(1)});
it('恢复查询只读取原请求，不再次提交',async()=>{const values=storage();values.set('pending-request','original');const network=vi.fn().mockResolvedValue({ok:true,json:async()=>({request_id:'original',status:'completed',result:{job_id:'saved-job'}})});vi.stubGlobal('fetch',network);expect((await recoverRequest())?.result?.job_id).toBe('saved-job');expect(network.mock.calls[0][1].method).toBe('GET');expect(values.has('pending-request')).toBe(false)});
it('未知结果不清除待核对标记',async()=>{const values=storage();vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,json:async()=>({request_id:'original',status:'outcome_unknown',result:null})}));await expect(act({action:'chat'})).rejects.toThrow('待核对');expect(values.has('pending-request')).toBe(true)});
