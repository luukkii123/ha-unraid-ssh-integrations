"""Fixed bounded inventory readers. No plugin code or password database loads."""
import shlex

# All file reads are bounded before parsing; only positively selected fields
# cross SSH. Optional source failures remain explicit nulls.
_COMMON = r'''
function bounded($path,$limit=65536) {
    $h=@fopen($path,"rb"); if ($h===false) return false;
    $s=stream_get_contents($h,$limit+1); fclose($h);
    return $s===false || strlen($s)>$limit ? false : $s;
}
function ini($path,$sections=true,$limit=65536) {
    $s=bounded($path,$limit); return $s===false ? false : @parse_ini_string($s,$sections,INI_SCANNER_RAW);
}
function pick($row,$keys) { return array_intersect_key($row,array_flip($keys)); }
'''
_USERS = r'''
$users=ini("/var/local/emhttp/users.ini"); if ($users===false) exit(1);
$out=[];
foreach ($users as $name=>$row) {
    if (!is_array($row)) exit(1);
    $out[]=["name"=>$row["name"] ?? (string)$name,"desc"=>$row["desc"] ?? "","passwd"=>($row["passwd"] ?? "") === "yes" ? "yes" : "no"];
}
$var=ini("/var/local/emhttp/var.ini",false);
$sec=ini("/var/local/emhttp/sec.ini"); $rights=null;
if (is_array($sec)) {
    $rights=[];
    foreach ($sec as $name=>$row) if (is_array($row)) $rights[$name]=pick($row,["export","security","readList","writeList"]);
}
$shares=ini("/var/local/emhttp/shares.ini"); $names=[];
if (is_array($shares)) foreach ($shares as $name=>$row) $names[]=$row["name"] ?? (string)$name;
else $rights=null;
$disks=ini("/var/local/emhttp/disks.ini"); $disknames=null;
if (is_array($disks)) {
    $disknames=[];
    foreach ($disks as $name=>$row) {
        $logical=$row["name"] ?? (string)$name;
        if (preg_match('/^disk[0-9]+$/D',$logical)) $disknames[]=$logical;
    }
}
echo json_encode(["users"=>$out,"var"=>is_array($var)?pick($var,["shareSMBEnabled","shareDisk","fsState"]):null,"rights"=>$rights===null?null:(object)$rights,"shares"=>$names,"disks"=>$disknames],JSON_THROW_ON_ERROR);
'''


_USB = r'''
$out=[];
$root="/sys/bus/usb/devices";
if (!is_dir($root) || !is_readable($root)) exit(1);
$paths=glob($root."/*"); if ($paths===false) exit(1);
foreach ($paths as $path) {
    $port=basename($path); if (str_contains($port,":")) continue;
    if (!file_exists($path."/idVendor") || !file_exists($path."/idProduct")) continue;
    $row=["port"=>$port];
    foreach (["busnum","devnum","product","manufacturer","idVendor","idProduct","bDeviceClass","speed","authorized","usbip_status"] as $key) {
        if (!file_exists($path."/".$key)) continue;
        $value=bounded($path."/".$key); if ($value===false) exit(1);
        $row[$key]=trim($value);
    }
    $driver=@readlink($path."/driver"); $row["driver"]=$driver===false?null:basename($driver);
    $out[]=$row;
}
echo json_encode($out,JSON_THROW_ON_ERROR);
'''
_CORAL = r'''
$out=[]; $root="/sys/class/apex";
if (!file_exists($root)) { echo "[]"; exit(0); }
if (!is_dir($root) || !is_readable($root)) exit(1);
$paths=glob($root."/apex_*"); if ($paths===false) exit(1);
foreach ($paths as $path) {
    if (!preg_match('/^apex_[0-9]+$/D',basename($path))) continue;
    $row=["path"=>$root."/".basename($path)];
    foreach (["status","temp","pci_address","driver_version"] as $key) {
        if (!file_exists($path."/".$key)) continue;
        $value=bounded($path."/".$key); if ($value===false) exit(1);
        $row[$key]=trim($value);
    }
    $out[]=$row;
}
echo json_encode($out,JSON_THROW_ON_ERROR);
'''
_MAPPING = r'''
// Active domains include paused VMs; inactive definitions are excluded. The XML never crosses SSH.
function run_bounded($command) {
    $h=popen($command,"r"); if ($h===false) return false;
    $text=stream_get_contents($h,65537); $status=pclose($h);
    return $status!==0 || $text===false || strlen($text)>65536 ? false : $text;
}
$text=run_bounded("timeout --foreground 0.4 virsh list --name");
if ($text===false) exit(1); $names=explode("\n",trim($text));
$active=[];
foreach ($names as $name) {
    if ($name==="") continue;
    $text=run_bounded("timeout --foreground 0.4 virsh dumpxml --domain ".escapeshellarg($name));
    if ($text===false) exit(1);
    // DOM parsing is local only, with entity/network expansion disabled.
    if (!class_exists("DOMDocument")) exit(1);
    $doc=new DOMDocument();
    if (stripos($text,"<!DOCTYPE")!==false || !@$doc->loadXML($text,LIBXML_NONET)) exit(1);
    $xpath=new DOMXPath($doc);
    foreach ($xpath->query('/domain/devices/hostdev[@type="usb"]/source') as $node) {
        $row=["vm"=>$name];
        foreach (["address"=>["bus","device"],"vendor"=>["id"],"product"=>["id"]] as $tag=>$attrs) {
            foreach ($node->getElementsByTagName($tag) as $child) foreach ($attrs as $attr) {
                $key=$tag==="address"?($attr==="device"?"dev":"bus"):$tag;
                if ($child->hasAttribute($attr)) {
                    $v=$child->getAttribute($attr);
                    $row[$key]=$tag==="address"?(string)intval($v,str_starts_with(strtolower($v),"0x")?16:10):strtolower(preg_replace('/^0x/i','',$v));
                }
            }
        }
        $active[]=$row;
    }
}
// These bounded capability probes are not current-connection authority.
$supported=false;
foreach (["/usr/local/emhttp/state/usb.ini","/var/local/emhttp/usb.ini"] as $path) {
    if (!file_exists($path)) continue;
    $cache=ini($path,true,1048576); if ($cache===false) exit(1);
    foreach ($cache as $row) if (is_array($row) && isset($row["bus"],$row["dev"],$row["USBPort"])) {
        $safe=pick($row,["bus","dev","USBPort","connected","VM"]); $supported=true;
    }
}
$remote="/tmp/usb_manager/config/usb_rmt_connect.cfg";
if (file_exists($remote)) {
    $cache=ini($remote,true,1048576); if ($cache===false) exit(1);
    // A port-only remote cache has no current device identity; omit its target.
    foreach ($cache as $row) if (is_array($row)) $safe=pick($row,["hostname","IP","port"]);
}
echo json_encode(["supported"=>$supported,"active"=>$active],JSON_THROW_ON_ERROR);
'''

def _php(script):
    return 'php -r ' + shlex.quote(_COMMON + script)


def inventory_sections() -> tuple[tuple[str, str], ...]:
    return (('inv_users', _php(_USERS)), ('inv_usb', _php(_USB)),
            ('inv_usb_mapping', _php(_MAPPING)), ('inv_coral', _php(_CORAL)))
