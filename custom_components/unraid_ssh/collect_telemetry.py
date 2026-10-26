"""Fixed, read-only dashboard probes; no optional plugin code is executed."""
import shlex


def _php(script):
    return 'php -r '+shlex.quote(script)


_HARDWARE = _php(r'''
$out = [];
foreach (["board_vendor","board_name","board_version","bios_vendor","bios_version","bios_date","product_name"] as $key) {
    $value = @file_get_contents("/sys/class/dmi/id/".$key);
    if ($value !== false) $out[$key] = trim($value);
}
$cpu = @file_get_contents("/proc/cpuinfo");
if ($cpu !== false && preg_match('/^model name\s*:\s*(.+)$/m', $cpu, $match)) $out["cpu_model"] = trim($match[1]);
if (!$out) exit(1);
echo json_encode((object)$out);
''')
_INTERFACES = _php(r'''
exec("ip -j -s link show", $lines, $status);
if ($status !== 0) exit(1);
$links = json_decode(implode("\n",$lines),true);
if (!is_array($links)) exit(1);
foreach ($links as &$link) {
    $name = $link["ifname"] ?? "";
    if (!preg_match('/^[a-zA-Z0-9_.:@-]+$/D',$name)) continue;
    $speed = @file_get_contents("/sys/class/net/".$name."/speed");
    if ($speed !== false && (int)$speed > 0) $link["speed_mbps"] = (int)$speed;
    $link["physical"] = is_link("/sys/class/net/".$name."/device");
    unset($link["address"],$link["broadcast"],$link["permaddr"]);
}
echo json_encode($links);
''')
_STORAGE = _php(r'''
exec("LC_ALL=C df -B1 --output=target,size,used,avail /boot /var/log /var/lib/docker",$lines,$status);
if ($status !== 0) exit(1);
$out = [];
foreach (array_slice($lines,1) as $line) {
    $cols = preg_split('/\s+/',trim($line));
    if (count($cols) === 4) $out[] = ["target"=>$cols[0],"size"=>(int)$cols[1],"used"=>(int)$cols[2],"avail"=>(int)$cols[3]];
}
echo json_encode($out);
''')
_SMART = _php(r'''
$disks = @parse_ini_file("/var/local/emhttp/disks.ini",true,INI_SCANNER_RAW);
if ($disks === false) exit(1);
$out = [];
foreach ($disks as $key=>$disk) {
    $name = $disk["name"] ?? trim($key,'"');
    if (!preg_match('/^[a-zA-Z0-9_.-]+$/D',$name) || empty($disk["device"])) continue;
    $file = "/var/local/emhttp/smart/".$name;
    $size = @filesize($file);
    if ($size === false || $size > 262144) continue;
    $text = @file_get_contents($file,false,null,0,262144);
    if ($text === false) continue;
    $out[$name] = ["text"=>$text,"mtime"=>@filemtime($file),"stale"=>($disk["spundown"] ?? "0") === "1"];
}
echo json_encode((object)$out);
''')
# Only bounded, read-only filesystem tools. Membership comes from explicit pool
# metadata and filesystem output, never from a numeric name suffix.
_POOLS = _php(r'''
$disks = @parse_ini_file("/var/local/emhttp/disks.ini",true,INI_SCANNER_RAW);
if ($disks === false) exit(1);
$out=[]; $carriers=[]; $deviceNames=[];
foreach ($disks as $key=>$disk) if (!empty($disk["device"])) $deviceNames[basename($disk["device"])]=$disk["name"] ?? trim($key,'"');
function public_member($path,$deviceNames) {
    $real=@realpath($path);
    $device=basename($real === false ? $path : $real);
    if (isset($deviceNames[$device])) return $deviceNames[$device];
    // Persistent /dev/disk/by-id aliases can contain model and serial numbers.
    // Only canonical Linux block identifiers may survive without an ini match.
    if (preg_match('/^(?:sd[a-z]+[0-9]*|vd[a-z]+[0-9]*|hd[a-z]+[0-9]*|nvme[0-9]+n[0-9]+(?:p[0-9]+)?|mmcblk[0-9]+(?:p[0-9]+)?|dm-[0-9]+)$/D',$device)) return $device;
    return null;
}
foreach ($disks as $key=>$disk) {
    if (($disk["type"] ?? "") !== "Cache") continue;
    $mount=$disk["fsMountpoint"] ?? "";
    if ($mount === "") continue;
    $disk["name"]=$disk["name"] ?? trim($key,'"');
    if (!isset($carriers[$mount]) || ((int)($disk["fsSize"] ?? 0)>0 && (int)($carriers[$mount]["fsSize"] ?? 0)===0)) $carriers[$mount]=$disk;
}
foreach ($carriers as $mount=>$disk) {
    $name=$disk["name"];
    $fs=$disk["fsType"] ?? "";
    $state=($disk["fsStatus"] ?? "") === "Mounted" ? "unknown" : "unmounted";
    $members=[];
    $metadata=$disk["devices"] ?? "";
    $expected=ctype_digit($metadata) ? (int)$metadata : null;
    if ($expected === null) foreach (preg_split('/[ ,]+/',$metadata,-1,PREG_SPLIT_NO_EMPTY) as $member) { $public=public_member($member,$deviceNames); if ($public !== null) $members[]=$public; }
    if ($state !== "unmounted" && $fs === "btrfs") {
        exec("timeout 1 btrfs filesystem show ".escapeshellarg($mount),$show,$rc);
        if ($rc === 0) {
            $state="online";
            foreach ($show as $line) {
                if (preg_match('/path\s+(\S+)/',$line,$m)) { $public=public_member($m[1],$deviceNames); if ($public !== null) $members[]=$public; else $state="unknown"; }
                if (stripos($line,"missing") !== false) $state="degraded";
            }
            exec("timeout 1 btrfs device stats ".escapeshellarg($mount),$stats,$statsrc);
            if ($statsrc !== 0) $state="unknown";
            elseif ($state === "online") foreach ($stats as $line) if (preg_match('/\s([1-9][0-9]*)$/',$line)) $state="warning";
            unset($stats);
        }
        unset($show);
    } elseif ($state !== "unmounted" && $fs === "zfs") {
        exec("timeout 1 zpool list -H -p -o name,health ".escapeshellarg($name),$health,$rc);
        exec("timeout 1 zpool status -P ".escapeshellarg($name),$show,$statusrc);
        if ($rc === 0 && $statusrc === 0 && isset($health[0])) {
            $cols=preg_split('/\s+/',trim($health[0]));
            $state=["ONLINE"=>"online","DEGRADED"=>"degraded","FAULTED"=>"faulted"][$cols[1] ?? ""] ?? "unknown";
            foreach ($show as $line) if (preg_match('#^\s+(/dev/\S+)\s+#',$line,$m)) { $public=public_member($m[1],$deviceNames); if ($public !== null) $members[]=$public; else $state="unknown"; }
        }
        unset($health,$show);
    }
    $members=array_values(array_unique($members));
    if ($state === "online" && $expected !== null && count($members)<$expected) $state="degraded";
    $total=isset($disk["fsSize"]) && ctype_digit($disk["fsSize"]) ? (int)$disk["fsSize"]*1024 : null;
    $free=isset($disk["fsFree"]) && ctype_digit($disk["fsFree"]) ? (int)$disk["fsFree"]*1024 : null;
    if ($total === null || $free === null || $free>$total) $total=$free=null;
    $out[]=["name"=>$name,"filesystem"=>$fs,"state"=>$state,"members"=>array_values(array_unique($members)),"total_bytes"=>$total,"used_bytes"=>$total===null?null:$total-$free,"free_bytes"=>$free];
}
echo json_encode($out);
''')
_SHARES = _php(r'''
$shares=@parse_ini_file("/var/local/emhttp/shares.ini",true,INI_SCANNER_RAW);
$sec=@parse_ini_file("/var/local/emhttp/sec.ini",true,INI_SCANNER_RAW);
if ($shares === false || $sec === false) exit(1);
$normalized=[];
foreach ($sec as $key=>$value) $normalized[trim($key,'"')]=$value;
$sec=$normalized;
foreach ($shares as $key=>$share) {
    $name=$share["name"] ?? trim($key,'"');
    echo "[".json_encode($name)."]\n";
    foreach (["export","security"] as $key) if (isset($sec[$name][$key])) echo $key."=".json_encode($sec[$name][$key])."\n";
}
''')
_STREAMS = _php(r'''
$shares=@parse_ini_file("/var/local/emhttp/shares.ini",true,INI_SCANNER_RAW);
$disks=@parse_ini_file("/var/local/emhttp/disks.ini",true,INI_SCANNER_RAW);
if ($shares === false || $disks === false) exit(1);
$mounts=[];
foreach ($disks as $disk) if (($disk["fsStatus"] ?? "") === "Mounted" && !empty($disk["fsMountpoint"]) && in_array($disk["type"] ?? "",["Data","Cache"],true)) $mounts[]=$disk["fsMountpoint"];
if (!$mounts) { echo "{}"; exit(0); }
$command="timeout 3 lsof -Fn";
foreach (array_unique($mounts) as $mount) $command.=" ".escapeshellarg($mount);
exec($command,$lines,$rc);
if ($rc !== 0) exit(1);
$out=[];
foreach ($shares as $key=>$share) {
    $name=$share["name"] ?? trim($key,'"');
    $paths=[];
    foreach ($lines as $line) {
        if (!str_starts_with($line,"n")) continue;
        $path=substr($line,1);
        foreach ($mounts as $mount) if (str_starts_with($path,rtrim($mount,"/")."/".$name."/")) $paths[$path]=true;
    }
    $out[$name]=count($paths);
}
echo json_encode((object)$out);
''')
_POWER = ('for p in /sys/class/powercap/*; do [ -r "$p/name" ] || continue; '
          'n=$(cat "$p/name") || continue; case "$n" in package-*) '
          'e=$(cat "$p/energy_uj") || continue; r=$(cat "$p/max_energy_range_uj") || continue; '
          'b=$(cat "$p/max_power_range_uw" 2>/dev/null); '
          'printf "%s\\t%s\\t%s\\t%s\\t%s\\n" "${p##*/}" "$n" "$e" "$r" "$b";; esac; done')


def dashboard_sections(*, include_inventory: bool, include_streams: bool) -> tuple[tuple[str,str], ...]:
    sections=[('dash_cpu','cat /proc/stat'),('dash_memory','cat /proc/meminfo'),('dash_interfaces',_INTERFACES),('dash_power',_POWER),('dash_storage',_STORAGE),('dash_mounts','cat /proc/self/mountinfo'),('dash_docker',"grep '^DOCKER_IMAGE_TYPE=' /boot/config/docker.cfg")]
    if include_inventory:
        sections.extend((('dash_hardware',_HARDWARE),('dash_smart',_SMART),('dash_pools',_POOLS),('dash_shares',_SHARES),('dash_gpu','nvidia-smi --query-gpu=index,driver_version --format=csv,noheader,nounits')))
    if include_streams: sections.append(('dash_streams',_STREAMS))
    requested='\n'.join(name for name,_ in sections)
    return (('dash_requested','printf %s '+shlex.quote(requested)),*sections)
