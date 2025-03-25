import gdb
import subprocess
import re
from pathlib import Path
from typing import Dict, Tuple, Optional


# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # Combined styles
    INFO = BOLD + BLUE + "[*] " + RESET
    SUCCESS = BOLD + GREEN + "[+] " + RESET
    WARNING = BOLD + YELLOW + "[W] " + RESET
    ERROR = BOLD + RED + "[E] " + RESET
    COMMAND = BOLD + MAGENTA + "[O] " + RESET

    @staticmethod
    def section(name: str) -> str:
        """Return colored section name"""
        return Colors.BOLD + Colors.CYAN + name + Colors.RESET

    @staticmethod
    def offset(addr: str) -> str:
        """Return colored offset address"""
        return Colors.BOLD + Colors.YELLOW + addr + Colors.RESET


LIBC_REGEX = re.compile(r"/libc-?([0-9]\.\d{2})?.so")
SECTION_PATTERN = re.compile(r"\s*\[\s*\d+\]\s+(\S+)\s+\S+\s+\S+\s+(\S+)")
BUILD_ID_PATTERN = re.compile(r".*Build ID.*:\s+(\w+)")

# Constants
SECTIONS = [".text", ".rodata", ".data", ".bss"]


class FetchCMD(gdb.Command):
    """
    fetch-debug - Fetch and set glibc debug info, especially for glibc-all-in-one

    Usage:
        fetch-debug [PATH]

    Argument:
        [PATH] - PATH to search for debug symbols. If not provided:
                 1. Uses $DEBUGDIR if set
                 2. Falls back to "{ELF_libc_path}/.debug" directory

    Example:
        fetch-debug /usr/share/glibc-all-in-one/libs/2.31-ubuntu9_amd64/.debug
    """

    def __init__(self) -> None:
        super(FetchCMD, self).__init__("fetch-debug", gdb.COMMAND_USER)

    def invoke(self, argument: str, from_tty: bool) -> None:
        execute(argument)


def execute(argument: str):
    try:
        path = get_debug_directory(argument)
        libc_result = get_libc()

        if libc_result is None:
            print(f"{Colors.ERROR}Cannot continue: libc not found")
            return

        libc_path, libc_base = libc_result

        if not path:
            path = libc_path.parent / ".debug"

        sections = get_section_by_path(libc_path)

        if not sections:
            print(f"{Colors.ERROR}No sections found")
            return

        filtered_sections = [
            (name, offset) for name, offset in sections.items() if name in SECTIONS
        ]

        if not filtered_sections:
            print(f"{Colors.WARNING}No required sections found ({', '.join(SECTIONS)})")
            return

        debug_id = get_debug_id(libc_path)
        if not debug_id:
            print(f"{Colors.ERROR}Could not get debug ID")
            return

        debug_path = path / ".build-id" / debug_id[:2] / (debug_id[2:] + ".debug")

        if not debug_path.exists():
            raise gdb.GdbError(f"{Colors.ERROR}Debug file '{debug_path}' not exists")

        final_command = ["add-symbol-file", debug_path.absolute().as_posix()]
        appends = []

        print(
            f"{Colors.INFO}Loading debug symbols from: {Colors.BOLD}{debug_path}{Colors.RESET}"
        )

        for section, offset in filtered_sections:
            addr_hex = f"0x{libc_base + offset:08x}"
            print(
                f"{Colors.SUCCESS}Dumping {Colors.section(section)} at {Colors.offset(addr_hex)}"
            )
            if section == ".text":
                final_command.append(f"0x{libc_base + offset:x}")
            else:
                appends.append(f"-s {section} 0x{libc_base + offset:x}")

        final_command.extend(appends)
        cmd_str = " ".join(final_command)
        print(f"{Colors.COMMAND}{cmd_str}")

        gdb.execute(cmd_str)
        print(f"{Colors.SUCCESS}Debug symbols loaded successfully!")

    except Exception as e:
        print(f"{Colors.ERROR}Error during execution: {str(e)}")


def get_debug_directory(args: str) -> Optional[Path]:
    """
    Get debug directory path

    Args:
        args: Command line arguments

    Returns:
        Debug directory path, or None if not set or doesn't exist
    """
    if arg := gdb.string_to_argv(args):
        debugdir = arg[0]
    elif debugdir_var := gdb.convenience_variable("DEBUGDIR"):
        debugdir = debugdir_var.string()
    else:
        debugdir = None

    if debugdir and (debug_path := Path(debugdir)).exists():
        return debug_path

    print(f"{Colors.WARNING}DEBUGDIR not set, using libc path")
    return None


def get_libc() -> Optional[Tuple[Path, int]]:
    """
    Get libc path and base address from process mappings

    Returns:
        Tuple of (libc_path, base_address), or None if not found
    """
    try:
        mappings = gdb.execute("info proc mappings", to_string=True).splitlines()

        for line in mappings:
            if LIBC_REGEX.search(line):
                parts = line.split()
                libc_path = parts[-1]
                libc_base = int(parts[0], 16)
                return Path(libc_path), libc_base

        print(f"{Colors.ERROR}No libc found in process mappings")
        return None
    except Exception as e:
        print(f"{Colors.ERROR}Error getting libc info: {str(e)}")
        return None


def get_debug_id(file: Path) -> str:
    """
    Get debug ID from ELF file

    Args:
        file: ELF file path

    Returns:
        Debug ID string, or empty string if not found
    """
    try:
        cmd = ["readelf", "-n", str(file)]
        result = subprocess.check_output(
            cmd, text=True, stderr=subprocess.PIPE
        ).splitlines()

        for line in result:
            if match := BUILD_ID_PATTERN.match(line):
                return match.group(1)

        print(f"{Colors.WARNING}No Build ID found in {file}")
        return ""
    except subprocess.CalledProcessError as e:
        print(f"{Colors.ERROR}Failed to execute readelf: {e.stderr}")
        return ""
    except Exception as e:
        print(f"{Colors.ERROR}Error getting debug ID: {str(e)}")
        return ""


def get_section_by_file(file: Path) -> Dict[str, int]:
    """
    Get section information from ELF file

    Args:
        file: ELF file path

    Returns:
        Dictionary mapping section names to offsets
    """
    result = {}
    try:
        cmd = ["readelf", "-S", str(file)]
        sections = subprocess.check_output(
            cmd, text=True, stderr=subprocess.PIPE
        ).splitlines()

        # Find section header row
        header_index = next(
            (i for i, line in enumerate(sections) if "[Nr] Name" in line), -1
        )

        if header_index == -1:
            print(f"{Colors.WARNING}No section header found in {file}")
            return result

        # Parse each section information line
        for line in sections[header_index + 1 :]:
            if match := SECTION_PATTERN.search(line):
                section_name = match.group(1)
                offset_hex = match.group(2)

                # Skip empty sections and sections with offset 0
                if section_name and offset_hex != "000000":
                    try:
                        offset = int(offset_hex, 16)
                        result[section_name] = offset
                    except ValueError:
                        pass  # Ignore offsets that cannot be parsed

        return result
    except subprocess.CalledProcessError as e:
        print(f"{Colors.ERROR}Failed to execute readelf: {e.stderr}")
        return result
    except Exception as e:
        print(f"{Colors.ERROR}Error getting section info: {str(e)}")
        return result


def get_section_by_path(path: Path) -> Dict[str, int]:
    """
    Get section information for the file at the specified path

    Args:
        path: ELF file path

    Returns:
        Dictionary with section names as keys and offsets as values
    """
    if not path.exists():
        raise gdb.GdbError(f"{Colors.ERROR}File '{path}' not exists")

    return get_section_by_file(path)


# Initialize command
FetchCMD()

# For convenience, fetch debug symbols automatically if the variable set
if gdb.convenience_variable("FETCH_DEFAULT"):
    print(f"{Colors.INFO}Setting up automatic debug symbol fetching")
    gdb.events.stop.connect(lambda x: execute(""))
