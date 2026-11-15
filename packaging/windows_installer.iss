; Compile only through build_windows_installer.py after complete-payload validation.
; User projects live outside this installation. Never add UninstallDelete wildcards.
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef BootstrapperPath
  #error BootstrapperPath is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef BuildDirectory
  #error BuildDirectory is required
#endif
#ifndef OutputDirectory
  #error OutputDirectory is required
#endif
#ifndef OutputName
  #error OutputName is required
#endif

[Setup]
AppId=FigureWorkbench.Desktop
AppName=Figure Workbench
AppVersion={#AppVersion}
AppPublisher=Figure Workbench
AppPublisherURL=https://github.com/aper-ai/figure-workbench
AppSupportURL=https://github.com/aper-ai/figure-workbench/issues
DefaultDirName={autopf}\Figure Workbench
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
WizardStyle=modern
LicenseFile={#PayloadDir}\app\LICENSE
OutputDir={#OutputDirectory}
OutputBaseFilename={#OutputName}
Compression=lzma2/normal
SolidCompression=yes
DiskSpanning=no
TimeStampsInUTC=yes
UninstallDisplayIcon={app}\versions\{#BuildDirectory}\FigureWorkbench.exe
CloseApplications=no
RestartApplications=no

[Files]
Source: "{#BootstrapperPath}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
Source: "{#PayloadDir}\*"; DestDir: "{app}\versions\{#BuildDirectory}"; Flags: ignoreversion recursesubdirs createallsubdirs notimestamp

[Icons]
Name: "{autoprograms}\Figure Workbench"; Filename: "{app}\versions\{#BuildDirectory}\FigureWorkbench.exe"; WorkingDir: "{app}\versions\{#BuildDirectory}"

[Code]
const
  RuntimeKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasRuntimeVersion(Root: Integer): Boolean;
var
  Version: String;
  I, Dots: Integer;
  NonZero: Boolean;
begin
  Result := False;
  if not RegQueryStringValue(Root, RuntimeKey, 'pv', Version) or (Version = '') then Exit;
  Dots := 0;
  NonZero := False;
  for I := 1 to Length(Version) do
  begin
    if Version[I] = '.' then
      Dots := Dots + 1
    else if (Version[I] >= '0') and (Version[I] <= '9') then
    begin
      if Version[I] <> '0' then NonZero := True;
    end
    else
      Exit;
  end;
  Result := (Dots = 3) and NonZero;
end;

function WebView2Installed(): Boolean;
begin
  Result := HasRuntimeVersion(HKLM32) or HasRuntimeVersion(HKCU);
end;

function IsWithin(Path, Parent: String): Boolean;
begin
  Result := Pos(Lowercase(AddBackslash(ExpandFileName(Parent))),
                Lowercase(AddBackslash(ExpandFileName(Path)))) = 1;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Workspace, InstallDirectory: String;
begin
  Result := '';
  Workspace := ExpandConstant('{localappdata}\FigureWorkbench');
  InstallDirectory := ExpandConstant('{app}');
  if IsWithin(InstallDirectory, Workspace) or IsWithin(Workspace, InstallDirectory) then
  begin
    Result := 'Choose an application installation directory separate from the Figure Workbench project workspace.';
    Exit;
  end;
  if WebView2Installed() then Exit;
  if not WizardSilent() then
    if MsgBox('Microsoft Edge WebView2 Runtime is required. Setup will download and install it from Microsoft. An internet connection is required. Continue?',
              mbConfirmation, MB_OKCANCEL) <> IDOK then
    begin
      Result := 'WebView2 Runtime installation was cancelled.';
      Exit;
    end;
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'Microsoft WebView2 Runtime could not be started. Check the network connection and run Setup again.';
    Exit;
  end;
  if ResultCode = 3010 then NeedsRestart := True;
  if not WebView2Installed() then
    Result := 'Microsoft WebView2 Runtime installation did not complete. Check the network connection, restart Windows if requested, and run Setup again.';
end;
