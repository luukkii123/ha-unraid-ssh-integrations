"""Pure dashboard samples. Only the explicit public projections leave this module."""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import re
from typing import Any
from . import parse

HARDWARE_KEYS = ('board_vendor','board_name','board_version','bios_vendor','bios_version','bios_date','product_name','cpu_model')
FAST = ('dash_cpu','dash_memory','dash_interfaces','dash_power','dash_storage','dash_mounts','dash_docker')
INVENTORY = ('dash_hardware','dash_smart','dash_pools','dash_shares','dash_gpu')

@dataclass(frozen=True)
class DashboardTelemetry:
    cores: tuple[dict, ...] | None = None
    memory: dict | None = None
    interfaces: tuple[dict, ...] | None = None
    power_packages: tuple[dict, ...] | None = None
    storage: tuple[dict, ...] | None = None
    hardware: dict | None = None
    gpu_info: tuple[dict, ...] | None = None
    smart: tuple[dict, ...] | None = None
    pools: tuple[dict, ...] | None = None
    shares: tuple[dict, ...] | None = None
    vms: tuple[dict, ...] | None = None
    failed: frozenset[str] = frozenset()
    sample_times: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    registration: str | None = None
    known_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    docker_mode: str = "unknown"


def interface_key(name: str) -> str:
    return name.encode('utf-8').hex()


def _cpu(text):
    out = {}
    boot = None
    for line in text.splitlines():
        cols=line.split()
        if cols and cols[0]=='btime': boot=int(cols[1])
        if cols and re.fullmatch(r'cpu[0-9]+',cols[0]):
            values=tuple(int(v) for v in cols[1:9])
            if len(values)<5 or min(values)<0: raise ValueError('invalid CPU counters')
            out[int(cols[0][3:])]=values
    return out, boot


def _memory(text):
    row={k:int(v)*1024 for k,v in re.findall(r'^(\w+):\s+(\d+) kB',text,re.M)}
    total,available,free,buffers=(row[k] for k in ('MemTotal','MemAvailable','MemFree','Buffers'))
    cache=max(0,row['Cached']+row['SReclaimable']-row['Shmem'])
    system=total-cache-buffers-free
    if total<=0 or system<0 or available>total: raise ValueError('invalid RAM partition')
    return dict(total_bytes=total,available_bytes=available,free_bytes=free,cache_bytes=cache,buffers_bytes=buffers,system_bytes=system)


def _delta(value,old,elapsed):
    if elapsed<=0 or not isinstance(value,int) or not isinstance(old,int) or value<old or old<0: return None
    return (value-old)/elapsed


def _smart(text):
    rows=[]
    for name,item in json.loads(text).items():
        raw=item['text']
        state='unknown'
        if re.search(r'(?:test result|Health Status):\s*(FAILED|NOK)\b',raw,re.I): state='failed'
        elif re.search(r'(?:test result|Health Status):\s*(PASSED|OK)\b',raw,re.I): state='passed'
        elif re.search(r'^\s*(FAILED|NOK)\s*$',raw,re.M): state='failed'
        elif re.search(r'^\s*(PASSED|OK)\s*$',raw,re.M): state='passed'
        details=[]
        for line in raw.splitlines():
            if re.match(r'\s*(5|187|197|198)\s+\w+',line):
                cols=line.split()
                labels={'5':{'Reallocated_Sector_Ct','Reallocated_Sector_Count'},
                        '187':{'Reported_Uncorrect'},'197':{'Current_Pending_Sector'},
                        '198':{'Offline_Uncorrectable'}}
                if len(cols)>=10 and cols[1] in labels.get(cols[0],set()) and cols[-1].isdigit() and int(cols[-1])>0:
                    details.append({'field':cols[1], 'value':int(cols[-1])})
            match=re.match(r'\s*(Critical Warning|Media and Data Integrity Errors|Available Spare):\s*(0x[0-9a-f]+|[0-9,]+)',line,re.I)
            if match:
                value=int(match[2].replace(',',''),0 if match[2].startswith('0x') else 10)
                if (match[1].lower()!='available spare' and value>0): details.append({'field':match[1], 'value':value})
        spare=re.search(r'^Available Spare:\s*(\d+)%',raw,re.M)
        threshold=re.search(r'^Available Spare Threshold:\s*(\d+)%',raw,re.M)
        if spare and threshold and int(spare[1])<int(threshold[1]):
            details.append({'field':'Available Spare','value':int(spare[1]),'threshold':int(threshold[1])})
        if details and state=='passed': state='warning'
        rows.append(dict(name=name,status=state,sampled_at=item.get('mtime'),source='emhttp_cache',stale=bool(item.get('stale')),details=details))
    return tuple(rows)


def _vm_raw(text):
    result={}; row=None
    for line in text.splitlines():
        match=re.fullmatch(r"Domain: '(.*)'",line.strip())
        if match: row=result.setdefault(match[1],{}); continue
        if row is not None and '=' in line:
            key,value=line.strip().split('=',1)
            row[key]=int(value) if value.isdecimal() else value
    return result


def _device_rate(row, old, group, direction, elapsed):
    def devices(values):
        return {value:values.get(key[:-4]+direction) for key,value in values.items() if re.fullmatch(group+r'\.\d+\.name',key)}
    new,before=devices(row),devices(old)
    if not new or new.keys()!=before.keys(): return None
    rates=[_delta(value,before[key],elapsed) for key,value in new.items()]
    return sum(rates) if all(v is not None for v in rates) else None


def parse_dashboard_sections(sections: dict[str,str], previous: DashboardTelemetry | None, *, sampled_at: float) -> DashboardTelemetry:
    previous=previous or DashboardTelemetry()
    failed=set(previous.failed); times=dict(previous.sample_times); raw={}; values={}
    requested=set(sections.get('dash_requested','').split()) | set(FAST)
    def read(name,fn,field):
        if name not in sections and name not in requested:
            values[field]=getattr(previous,field)
            if name in previous.raw: raw[name]=previous.raw[name]
            return
        times[name]=sampled_at
        try:
            values[field]=fn(sections[name]); failed.discard(name)
        except (KeyError,ValueError,TypeError,IndexError,AttributeError):
            values[field]=None; failed.add(name)
    cpu={}; boot=None
    def cores(text):
        nonlocal cpu,boot
        cpu,boot=_cpu(text)
        raw['dash_cpu']=(cpu,boot)
        old,oldboot=previous.raw.get('dash_cpu',({},None))
        rows=[]
        for key,now in cpu.items():
            before=old.get(key); percent=None
            if before and boot is not None and boot==oldboot and len(before)==len(now):
                diff=[a-b for a,b in zip(now,before)]
                total=sum(diff)
                if min(diff)>=0 and total>0: percent=round((total-diff[3]-diff[4])/total*100,2)
            rows.append(dict(id=key,percent=percent))
        return tuple(rows)
    read('dash_cpu',cores,'cores')
    read('dash_memory',_memory,'memory')
    def interfaces(text):
        rows=[]; current={}; old=previous.raw.get('dash_interfaces',{})
        elapsed=sampled_at-previous.sample_times.get('dash_interfaces',sampled_at)
        for item in json.loads(text):
            name=item['ifname']; index=item['ifindex']; stats=item.get('stats64',item.get('stats',{}))
            rx=stats.get('rx',{}).get('bytes'); tx=stats.get('tx',{}).get('bytes')
            identity=(name,index); before=old.get(identity,(None,None)) if boot is not None and boot==previous.raw.get('dash_cpu',({},None))[1] else (None,None); current[identity]=(rx,tx)
            kind=item.get('linkinfo',{}).get('info_kind') or ('loopback' if item.get('link_type')=='loopback' else 'physical' if item.get('physical') else 'virtual')
            rows.append(dict(id=interface_key(name),name=name,kind=kind,state=str(item.get('operstate','unknown')).lower(),mtu=item.get('mtu'),speed_mbps=item.get('speed_mbps'),rx_bytes_s=_delta(rx,before[0],elapsed),tx_bytes_s=_delta(tx,before[1],elapsed)))
        raw['dash_interfaces']=current
        return tuple(rows)
    read('dash_interfaces',interfaces,'interfaces')
    def power(text):
        rows=[]; current={}; old=previous.raw.get('dash_power',{})
        elapsed=sampled_at-previous.sample_times.get('dash_power',sampled_at)
        oldboot=previous.raw.get('dash_cpu',({},None))[1]
        for line in text.splitlines():
            fields=line.split('\t')
            key,label,energy,limit=fields[:4]; energy=int(energy); limit=int(limit)
            power_bound=int(fields[4]) if len(fields)>4 and fields[4].isdecimal() else None
            if not re.fullmatch(r'package[- ]\d+',label,re.I): continue
            current[key]=(energy,limit,power_bound); before=old.get(key); watts=None
            if before and boot is not None and boot==oldboot and elapsed>0 and 0<=energy<limit and before[1]==limit and 0<=before[0]<limit and power_bound and before[2]==power_bound and elapsed*power_bound<limit:
                delta=energy-before[0]
                if delta<0: delta+=limit
                if delta <= elapsed*power_bound:
                    watts=delta/elapsed/1e6
            rows.append(dict(id=key,power_w=watts,source='rapl'))
        raw['dash_power']=current
        return tuple(rows)
    read('dash_power',power,'power_packages')
    docker=parse.parse_flat_ini(sections.get('dash_docker','')).get('DOCKER_IMAGE_TYPE')
    # Public Unraid raw values: empty=btrfs image, xfs=image, folder=directory.
    # Missing and unrecognized keys remain distinct from explicitly empty.
    docker_mode='image' if docker in {'','xfs'} else 'folder' if docker=='folder' else 'unknown'
    if 'dash_docker' not in sections: failed.add('dash_docker')
    else: failed.discard('dash_docker'); times['dash_docker']=sampled_at
    def storage(text):
        mounts=set()
        for line in sections['dash_mounts'].splitlines():
            parts=line.split()
            if len(parts)>4: mounts.add(parts[4])
        result=[]
        for item in json.loads(text):
            kind={'/boot':'boot','/var/log':'log','/var/lib/docker':'docker_vdisk'}.get(item['target'])
            if not kind or item['target'] not in mounts or (kind=='docker_vdisk' and docker_mode!='image'): continue
            total,used,free=(int(item[k]) for k in ('size','used','avail'))
            if total<=0 or min(used,free)<0 or max(used,free)>total: continue
            result.append(dict(kind=kind,total_bytes=total,used_bytes=used,free_bytes=free,percent=used/total*100))
        failed.discard('dash_mounts')
        return tuple(result)
    read('dash_storage',storage,'storage')
    if 'dash_mounts' not in sections: failed.add('dash_mounts')
    read('dash_hardware',lambda t:{k:str(v) for k,v in json.loads(t).items() if k in HARDWARE_KEYS},'hardware')
    def smart(text):
        rows=list(_smart(text))
        names={row['name'] for row in rows}
        for disk in parse.parse_disks(sections.get('disks','')):
            if disk.name not in names:
                rows.append(dict(name=disk.name,status='unknown',sampled_at=None,source='emhttp_cache',stale=True,details=[]))
        return tuple(rows)
    read('dash_smart',smart,'smart')
    def pools(text):
        result=[]
        for item in json.loads(text):
            result.append({key:item.get(key) for key in ('name','filesystem','state','members','total_bytes','used_bytes','free_bytes')})
        return tuple(result)
    read('dash_pools',pools,'pools')
    def shares(text):
        sec=json.loads(text); result=[]
        if not isinstance(sec,dict): raise ValueError('invalid share metadata')
        for share in parse.parse_shares(sections['shares']):
            info=sec.get(share.name,{})
            security=info.get('security','unknown').lower()
            exported=info.get('export')
            result.append(dict(name=share.name,exported=None if exported is None else exported.lower() not in {'no','-','0'},security=security if security in {'public','secure','private'} else 'unknown',streams=None))
        return tuple(result)
    read('dash_shares',shares,'shares')
    if 'dash_streams' in sections or 'dash_streams' in requested:
        times['dash_streams']=sampled_at
        try:
            counts=json.loads(sections['dash_streams'])
            if not isinstance(counts,dict) or any(not isinstance(key,str) or type(value) is not int or value<0 for key,value in counts.items()):
                raise ValueError('invalid stream counts')
            raw['dash_streams']=counts; failed.discard('dash_streams')
        except (KeyError,ValueError,TypeError): counts={}; failed.add('dash_streams')
    else:
        counts=previous.raw.get('dash_streams',{})
        if 'dash_streams' in previous.raw and 'dash_streams' not in previous.failed:
            raw['dash_streams']=counts
    if values['shares'] is not None: values['shares']=tuple({**row,'streams':counts.get(row['name'])} for row in values['shares'])
    def gpu(text):
        rows=[]
        names={g.index:g.name for g in parse.parse_gpus(sections.get('gpu',''))}
        for line in text.splitlines():
            index,driver=line.split(',',1); index=int(index)
            rows.append(dict(index=index,model=names.get(index),driver_version=driver.strip()))
        return tuple(rows)
    read('dash_gpu',gpu,'gpu_info')
    try:
        vmrows=_vm_raw(sections['vm_stats']); oldrows=previous.raw.get('vm_stats',{})
        instances={v.name:v.runtime_id for v in parse.parse_vms(sections['vms']) if v.state=='running'}
        elapsed=sampled_at-previous.sample_times.get('vm_stats',sampled_at)
        result=[]
        for name,runtime in instances.items():
            row=vmrows.get(name,{}); old=oldrows.get(name,{}) if runtime is not None and previous.raw.get('vms',{}).get(name)==runtime and boot is not None and boot==previous.raw.get('dash_cpu',({},None))[1] else {}
            cpu_rate=_delta(row.get('cpu.time'),old.get('cpu.time'),elapsed)
            result.append(dict(name=name,host_cpu_percent=cpu_rate/1e9*100/len(cpu) if cpu_rate is not None and cpu else None,
                net_rx_bytes_s=_device_rate(row,old,'net','rx.bytes',elapsed),net_tx_bytes_s=_device_rate(row,old,'net','tx.bytes',elapsed),
                disk_read_bytes_s=_device_rate(row,old,'block','rd.bytes',elapsed),disk_write_bytes_s=_device_rate(row,old,'block','wr.bytes',elapsed)))
        values['vms']=tuple(result); raw['vm_stats']=vmrows; raw['vms']=instances; times['vm_stats']=sampled_at; failed.discard('vm_stats')
    except (KeyError,ValueError,TypeError): values['vms']=None; failed.add('vm_stats')
    known_ids=dict(previous.known_ids)
    for source, field_name, key in (("dash_interfaces","interfaces","id"),("dash_smart","smart","name")):
        rows=values[field_name]
        if rows is not None:
            known_ids[source]=tuple(row[key] for row in rows)
    registration=parse.parse_flat_ini(sections.get('var','')).get('regTy')
    return DashboardTelemetry(**values,registration=registration,failed=frozenset(failed),sample_times=times,raw=raw,known_ids=known_ids,docker_mode=docker_mode)
