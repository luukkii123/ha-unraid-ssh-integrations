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


def _php(script):
    return 'php -r ' + shlex.quote(_COMMON + script)


def inventory_sections() -> tuple[tuple[str, str], ...]:
    return (('inv_users', _php(_USERS)),)
