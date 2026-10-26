; Integrated x64 preview installer. Upstream signed runtime installers are
; embedded intact. Never repack or modify their Microsoft-signed drivers.
[Setup]
AppId={{4BF513F2-90F1-4FC8-A4D2-05DB24199428}
AppName=Portway Connector
AppVersion=0.4.0
AppPublisher=Portway
DefaultDirName={autopf}\Portway
DefaultGroupName=Portway
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
PrivilegesRequired=admin
MinVersion=10.0.18362
OutputDir=..\dist
OutputBaseFilename=PortwayConnectorSetup
Compression=lzma2
SolidCompression=yes
CloseApplications=no
RestartApplications=no
AlwaysRestart=yes
AppMutex=Local\PortwayConnector
UninstallDisplayIcon={app}\PortwayConnector.exe
WizardStyle=modern
InfoBeforeFile=preview-notice.txt

[Files]
Source: "..\dist\PortwayConnector.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\native\install-runtime.ps1"; DestDir: "{tmp}\PortwayRuntime"; Flags: deleteafterinstall
Source: "..\native\packages.lock.json"; DestDir: "{tmp}\PortwayRuntime"; Flags: deleteafterinstall
Source: "..\native\packages\usbipd-win_5.3.0_x64.msi"; DestDir: "{tmp}\PortwayRuntime\packages"; Flags: deleteafterinstall
Source: "..\native\packages\USBip-0.9.8.1-x64.exe"; DestDir: "{tmp}\PortwayRuntime\packages"; Flags: deleteafterinstall
Source: "..\native\licenses\*"; DestDir: "{app}\OpenSource"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Portway Connector"; Filename: "{app}\PortwayConnector.exe"
Name: "{autodesktop}\Portway Connector"; Filename: "{app}\PortwayConnector.exe"

[Code]
var
  RuntimeRestart: Boolean;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Args: String;
begin
  if CurStep = ssPostInstall then
  begin
    Args := '-NoProfile -NonInteractive -WindowStyle Hidden -File "' +
      ExpandConstant('{tmp}\PortwayRuntime\install-runtime.ps1') +
      '" -LogDirectory "' + ExpandConstant('{commonappdata}\Portway\InstallerLogs') + '"';
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Args,
      '', SW_HIDE, ewWaitUntilTerminated, Code) then
      RaiseException('Could not start the signed USB runtime installation.');
    if (Code <> 0) and (Code <> 3010) then
      RaiseException('USB runtime installation failed. See C:\ProgramData\Portway\InstallerLogs.');
    RuntimeRestart := Code = 3010;
  end;
end;

function NeedRestart(): Boolean;
begin
  Result := RuntimeRestart;
end;
