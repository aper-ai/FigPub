"""Read an existing Windows shortcut through WScript and Unicode IShellLinkW.

This diagnostic never launches, resolves, saves or changes the shortcut. It uses
only Python's standard library and the Windows COM interfaces exposed by pwsh.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess


# IShellLinkW uses the SDK's IUnknown vtable order and explicit UTF-16 buffers:
# https://learn.microsoft.com/windows/win32/api/shobjidl_core/nn-shobjidl_core-ishelllinkw
# IPersistFile.Load is the framework's existing COM declaration, with STGM_READ=0:
# https://learn.microsoft.com/dotnet/api/system.runtime.interopservices.comtypes.ipersistfile.load
_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PATH_BASE64__'))
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace FigureWorkbench.ShortcutDiagnostics {
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellLinkW {
        [PreserveSig] int GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path,
            int capacity, IntPtr findData, uint flags);
        [PreserveSig] int GetIDList(out IntPtr pidl);
        [PreserveSig] int SetIDList(IntPtr pidl);
        [PreserveSig] int GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder description,
            int capacity);
        [PreserveSig] int SetDescription([MarshalAs(UnmanagedType.LPWStr)] string description);
        [PreserveSig] int GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder directory,
            int capacity);
        [PreserveSig] int SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string directory);
        [PreserveSig] int GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments,
            int capacity);
    }

    public static class Inspector {
        [DllImport("kernel32.dll")]
        public static extern uint GetACP();

        public static Dictionary<string, object> Read(string path) {
            const int Capacity = 260; // GetPath's documented MAX_PATH limit.
            const uint SLGP_RAWPATH = 0x00000004;
            object instance = Activator.CreateInstance(Type.GetTypeFromCLSID(
                new Guid("00021401-0000-0000-C000-000000000046"), true));
            try {
                ((IPersistFile)instance).Load(path, 0); // STGM_READ; never Save/Resolve.
                IShellLinkW link = (IShellLinkW)instance;
                var target = new StringBuilder(Capacity);
                var raw = new StringBuilder(Capacity);
                var directory = new StringBuilder(Capacity);
                var arguments = new StringBuilder(Capacity);
                int targetStatus = link.GetPath(target, Capacity, IntPtr.Zero, 0);
                int rawStatus = link.GetPath(raw, Capacity, IntPtr.Zero, SLGP_RAWPATH);
                int directoryStatus = link.GetWorkingDirectory(directory, Capacity);
                int argumentsStatus = link.GetArguments(arguments, Capacity);
                return new Dictionary<string, object> {
                    {"target", target.ToString()}, {"raw_target", raw.ToString()},
                    {"working_directory", directory.ToString()}, {"arguments", arguments.ToString()},
                    {"target_hresult", targetStatus}, {"raw_target_hresult", rawStatus},
                    {"working_directory_hresult", directoryStatus}, {"arguments_hresult", argumentsStatus},
                    {"target_flags", 0}, {"raw_target_flags", SLGP_RAWPATH}, {"buffer_capacity", Capacity}
                };
            } finally {
                if (instance != null && Marshal.IsComObject(instance))
                    Marshal.FinalReleaseComObject(instance);
            }
        }
    }
}
'@

$wscript = [ordered]@{target=$null;working_directory=$null;arguments=$null;error=$null}
$shell = $null
$link = $null
try {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($path)
    $wscript.target = [string]$link.TargetPath
    $wscript.working_directory = [string]$link.WorkingDirectory
    $wscript.arguments = [string]$link.Arguments
} catch {
    $wscript.error = $_.Exception.ToString()
} finally {
    if ($null -ne $link) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
    if ($null -ne $shell) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
}
$native = [ordered]@{target=$null;working_directory=$null;error=$null}
try {
    $native = [FigureWorkbench.ShortcutDiagnostics.Inspector]::Read($path)
    $native['error'] = $null
} catch {
    $native.error = $_.Exception.ToString()
}

# Preserve the strings before console/JSON transport as independently decodable
# UTF-16LE bytes, so '?' from COM can be distinguished from transport damage.
$transport = [ordered]@{}
foreach ($reader in @('wscript', 'shell_link_w')) {
    $values = if ($reader -eq 'wscript') { $wscript } else { $native }
    foreach ($field in @('target', 'working_directory', 'arguments', 'raw_target')) {
        $value = $values[$field]
        if ($null -ne $value) {
            $transport["${reader}.${field}"] = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes([string]$value))
        }
    }
}
[ordered]@{
    path=$path
    wscript=$wscript
    shell_link_w=$native
    transport_utf16le_base64=$transport
    powershell_version=$PSVersionTable.PSVersion.ToString()
    powershell_edition=$PSVersionTable.PSEdition
    output_encoding=[Console]::OutputEncoding.WebName
    ansi_code_page=[FigureWorkbench.ShortcutDiagnostics.Inspector]::GetACP()
    process_architecture=[Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
} | ConvertTo-Json -Depth 8 -Compress
"""


def inspect_shortcut(path: Path) -> dict:
    """Return both readers' unmodified strings and their Unicode diagnostics."""
    if os.name != 'nt':
        raise OSError('Windows shortcut inspection requires Windows.')
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() != '.lnk' or not path.is_file():
        raise ValueError('Choose an existing .lnk file to inspect.')
    original = path.read_bytes()
    path_base64 = base64.b64encode(str(path).encode('utf-8')).decode('ascii')
    script = _POWERSHELL.replace('__PATH_BASE64__', path_base64)
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    stdout = subprocess.check_output(
        ['pwsh.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        text=True, encoding='utf-8', errors='strict', timeout=60,
    )
    result = json.loads(stdout.lstrip('\ufeff'))
    result['transport_matches'] = all(
        base64.b64decode(value, validate=True).decode('utf-16le') == result[reader][field]
        for key, value in result['transport_utf16le_base64'].items()
        for reader, field in [key.split('.', 1)]
    )
    current = path.read_bytes()
    result['shortcut_file'] = {
        'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest(),
        'sha256_after_read': hashlib.sha256(current).hexdigest(),
        'unchanged_after_read': current == original,
    }
    # Keep literal returned values intact; code points aid mismatch diagnosis.
    result['code_points'] = {
        reader: {key: [ord(char) for char in values[key]]
                 for key in ('target', 'working_directory') if isinstance(values.get(key), str)}
        for reader in ('wscript', 'shell_link_w')
        for values in [result[reader]]
    }
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('shortcut', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect_shortcut(args.shortcut), ensure_ascii=True, indent=2))
