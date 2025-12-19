import sys
import os
import subprocess
import configparser
import json
from dataclasses import dataclass
from typing import Dict, List, NoReturn, Optional, Tuple
from logger import logger

@dataclass
class Quota: 
    space_used_bytes: Optional[int]
    space_soft_limit_bytes: Optional[int]
    space_hard_limit_bytes: Optional[int]
    inodes_used: Optional[int]
    inodes_soft_limit: Optional[int]
    inodes_hard_limit: Optional[int]


def _stop_with_error(msg: str, code: int = 1) -> NoReturn:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _parse_config(file_name, section):
    if not os.path.exists(file_name):
        _stop_with_error(f"could not find configuration file {file_name}")
    config = configparser.ConfigParser()
    config.read(file_name)
    if section not in config.sections():
        _stop_with_error(f"cluster '{section}' not correctly defined in {file_name}")
    return dict(config[section])


def _get_option(config, option):
    if option in config:
        return config[option]
    else:
        _stop_with_error(f"option {option} is not set correctly")

def _shell_command(command):
    try:
        output = (
            subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT)
            .decode("utf-8")
            .strip()
        )
    except subprocess.CalledProcessError as e:
        msg = e.output.decode("utf-8").strip()
        if not msg:
            msg = f"Command failed with exit code {e.returncode}: {command}"
        _stop_with_error(msg)
    return output

def _beegfs_name_to_paths(
        name: str, 
        account: str, 
        config: Dict[str, str], 
        groups: List[str]
    ) -> Optional[List[str]]:
    """ Map BeegFS quota name to file system path"""
    
    home_prefix = _get_option(config, "home_prefix")
    scratch_prefix = _get_option(config, "scratch_prefix")
    project_path_prefixes = _get_option(config, "project_path_prefixes").split(", ")

    if name == account:
        path = os.path.join(scratch_prefix, account)
        logger.debug(f"BeegFS: '{name}' is user -> {path}")
        return [path]

    if name == f"{account}_g":
        path = os.path.join(home_prefix, account)
        logger.debug(f"BeegFS: '{name}' is home -> {path}")
        return [path]

    if name in groups:
        paths = [path for _, path in _valid_project_paths([name], project_path_prefixes)]
        if paths:
            logger.debug(f"BeegFS: '{name}' is project -> {paths}")
            return paths
        logger.debug(f"BeegFS: '{name} is in groups but no path found")
        return None

    logger.debug(f"BeegFS: '{name}' did not match any quota entry type")
    return None

def _parse_beegfs_quota(quota_str: str) -> Tuple[Optional[int], Optional[int]]:
    try:
        used_str, limit_str = quota_str.split("/")

        used = _parse_beegfs_size(used_str)
        limit = None if limit_str == "∞" else _parse_beegfs_size(limit_str)

        return used, limit
    except Exception as e:
        _stop_with_error(f"Error parsing BeegFS quota string {quota_str}: {e}")

def _parse_beegfs_size(size_str: str) -> int:
    """Parse united units to unitless integers"""
    units = {
        "PiB": 1024**5,
        "TiB": 1024**4,
        "GiB": 1024**3,
        "MiB": 1024**2,
        "KiB": 1024,
        "P": 1000**5,
        "T": 1000**4,
        "G": 1000**3,
        "M": 1000**2,
        "k": 1000,
        "": 1,
    }
    try:
        for unit in sorted(units.keys(), key=len, reverse=True):
            if size_str.endswith(unit):
                num_str = size_str[:-len(unit)] if unit else size_str
                number = float(num_str)
                bytes_val = int(number * units[unit])
                logger.debug(f"BeegFS: size {size_str} -> {bytes_val}")
                return bytes_val
        raise Exception()
    except Exception as e:
        _stop_with_error(f"failed to parse BeegFS size string {size_str}: {e}")


def _beegfs_quota_for_current_user(config: Dict[str, str]) -> Dict[str, Quota]:
    """Get BeegFS quota for current user"""
    current_user = os.getenv("USER")
    logger.debug(f"BeegFS: querying quotas for current user '{current_user}'")
    if current_user is None:
        _stop_with_error("failed to get current user from environment variable USER")

    command = "beegfs quota list-usage --output ndjson"
    output = _shell_command(command).split("\n")
    
    groups = _shell_command(f"id -Gn {current_user}").split()
    logger.debug(f"BeegFS: groups for '{current_user}': {groups}")
    
    d = {}

    for line in output:
        line = line.strip()
        if not line or line.startswith("INFO: "):
            continue

        try:
            entry = json.loads(line)
            name = entry["name"]
            space_str = entry["space"]
            inode_str = entry["inode"]

            paths = _beegfs_name_to_paths(name, current_user, config, groups)
            if not paths:
                continue

            space_used, space_limit = _parse_beegfs_quota(space_str)
            inodes_used, inodes_limit = _parse_beegfs_quota(inode_str)

            quota = Quota(
                space_used_bytes=space_used,
                space_soft_limit_bytes=space_limit,
                space_hard_limit_bytes=space_limit,
                inodes_used=inodes_used,
                inodes_soft_limit=inodes_limit,
                inodes_hard_limit=inodes_limit,
            )

            for path in paths:
                d[path] = quota
                logger.debug(f"BeegFS: added {path}")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.debug(f"BeegFS: failed to parse {line}, {e}")
    
    return d


def _lustre_quota_using_command(command) -> Quota:
    output = _shell_command(command)

    (
        _,
        space_used_kib,
        space_soft_limit_kib,
        space_hard_limit_kib,
        _,
        inodes_used,
        inodes_soft_limit,
        inodes_hard_limit,
        _,
    ) = output.split()

    # lustre adds a "*" if we are beyond quota
    # here we remove that "*", otherwise it messes up the rest of the code
    space_used_kib = space_used_kib.replace("*", "")
    inodes_used = inodes_used.replace("*", "")

    # all space quota numbers are initially in KiB and we convert to bytes
    space_used_bytes = 1024 * int(space_used_kib)
    if space_soft_limit_kib == "0":
        space_soft_limit_bytes = None
    else:
        space_soft_limit_bytes = 1024 * int(space_soft_limit_kib)
    if space_hard_limit_kib == "0":
        space_hard_limit_bytes = None
    else:
        space_hard_limit_bytes = 1024 * int(space_hard_limit_kib)

    inodes_used = int(inodes_used)
    if inodes_soft_limit == "0":
        inodes_soft_limit = None
    else:
        inodes_soft_limit = int(inodes_soft_limit)
    if inodes_hard_limit == "0":
        inodes_hard_limit = None
    else:
        inodes_hard_limit = int(inodes_hard_limit)

    return Quota(space_used_bytes=space_used_bytes, 
                 space_soft_limit_bytes=space_soft_limit_bytes,
                 space_hard_limit_bytes=space_hard_limit_bytes, 
                 inodes_used=int(inodes_used),
                 inodes_soft_limit=inodes_soft_limit,
                 inodes_hard_limit=inodes_hard_limit)


def _lustre_quota_using_option(option, account, file_system_prefix):
    command = f"lfs quota -q -{option} {account} {file_system_prefix} | grep {file_system_prefix}"
    return _lustre_quota_using_command(command)


def _lustre_quota_using_path(path, file_system_prefix):
    project_id = int(_shell_command(f"lfs project -d {path} | awk '{{print $1}}'"))
    if project_id == 0:
        # workaround for projects that do not have quota set
        # in this case the path does not have quota and information would default
        # to project ID 0 which on our cluser gave space used by entire cluster
        return {
            path: Quota(space_used_bytes=None, 
                        space_soft_limit_bytes=None,
                        space_hard_limit_bytes=None,
                        inodes_used=None,
                        inodes_soft_limit=None,
                        inodes_hard_limit=None)
        }
    else:
        command = f"lfs quota -q -p {project_id} {file_system_prefix} | head -n 1"
        return {path: _lustre_quota_using_command(command)}


def _beegfs_quota_using_path(path, file_system_prefix):
    return {}


def _valid_project_paths(projects, project_path_prefixes):
    result = []
    for project in projects:
        for project_path_prefix in project_path_prefixes:
            path = os.path.join(project_path_prefix, project)
            if os.path.isdir(path):
                result.append((project, path))
    return result

def _quota_using_account(account, config, _quota_using_option, _quota_using_path):
    file_system_prefix = _get_option(config, "file_system_prefix")
    home_prefix = _get_option(config, "home_prefix")
    scratch_prefix = _get_option(config, "scratch_prefix")
    project_path_prefixes = _get_option(config, "project_path_prefixes").split(", ")
    path_based = _get_option(config, "path_based") == "yes"

    groups = _shell_command(f"id -Gn {account}").split()

    d = {}
    if path_based:
        d.update(
            _quota_using_path(os.path.join(home_prefix, account), file_system_prefix)
        )
        for _, path in _valid_project_paths(groups, project_path_prefixes):
            d.update(_quota_using_path(path, file_system_prefix))
    else:
        d.update(
            {file_system_prefix: _quota_using_option("u", account, file_system_prefix)}
        )
        d.update(
            {
                os.path.join(home_prefix, account): _quota_using_option(
                    "g", account + "_g", file_system_prefix
                )
            }
        )
        d.update(
            {
                os.path.join(scratch_prefix, account): _quota_using_option(
                    "g", account, file_system_prefix
                )
            }
        )
        for group, path in _valid_project_paths(groups, project_path_prefixes):
            d.update(_quota_using_path(path, file_system_prefix))
            d.update({path: _quota_using_option("g", group, file_system_prefix)})
    return d


def _quota_using_project(project, config, _quota_using_option, _quota_using_path):
    file_system_prefix = _get_option(config, "file_system_prefix")
    project_path_prefixes = _get_option(config, "project_path_prefixes").split(", ")
    path_based = _get_option(config, "path_based") == "yes"
    
    d = {}
    if path_based:
        for _, path in _valid_project_paths([project], project_path_prefixes):
            d.update(_quota_using_path(path, file_system_prefix))
    else:
        for group, path in _valid_project_paths([project], project_path_prefixes):
            d.update(_quota_using_path(path, file_system_prefix))
            d.update({path: _quota_using_option("g", group, file_system_prefix)})
    return d


def quota_using_path(config_file, cluster, path):
    config = _parse_config(config_file, cluster)
    file_system = _get_option(config, "file_system")
    file_system_prefix = _get_option(config, "file_system_prefix")

    if file_system == "lustre":
        return _lustre_quota_using_path(path, file_system_prefix)
    elif file_system == "beegfs":
        raise ValueError("path-based query not implemented for beegfs")
    else:
        _stop_with_error(f"file system {file_system} is not implemented")


def quota_using_project(config_file, cluster, project):
    config = _parse_config(config_file, cluster)
    file_system = _get_option(config, "file_system")

    if file_system == "lustre":
        _quota_using_option = _lustre_quota_using_option
        _quota_using_path = _lustre_quota_using_path
        return _quota_using_project(project, config, _quota_using_option, _quota_using_path)
    elif file_system == "beegfs":
        raise ValueError("project-based query not implemented for beegfs")



def quota_using_account(config_file, cluster, account) -> dict[str, Quota]:
    config = _parse_config(config_file, cluster)
    file_system = _get_option(config, "file_system")
    logger.debug(f"Looking for quota using account {account} on {cluster}")

    if file_system == "lustre":
        _quota_using_option = _lustre_quota_using_option
        _quota_using_path = _lustre_quota_using_path
        return _quota_using_account(account, config, _quota_using_option, _quota_using_path)
    elif file_system == "beegfs":
        current_user = os.getenv("USER")
        if account != current_user:
            raise ValueError(
                f"BeegFS: cannot query account '{account}' - "
                f"only the current user '{current_user}' can query their own quota"
            )
        return _beegfs_quota_for_current_user(config)
    else:
        _stop_with_error(f"file system {file_system} is not implemented")

def _debug_quota_using_account(config_file, cluster, account):
    return {
        "/cluster/home/somebody": Quota(inodes_hard_limit=110000,
                                        inodes_soft_limit=100000,
                                        inodes_used=90000,
                                        space_hard_limit_bytes=32212254720,
                                        space_soft_limit_bytes=21474836480,
                                        space_used_bytes=369164288),
        "/cluster/projects/nn1234k": Quota(inodes_hard_limit=1000000,
                                           inodes_soft_limit=1000000,
                                           inodes_used=1,
                                           space_hard_limit_bytes=1099511627776,
                                           space_soft_limit_bytes=1099511627776,
                                           space_used_bytes=800000000000)
    }


