; PC Cleaner installer (Inno Setup 6). Built automatically by GitHub Actions.
#ifndef MyVersion
  #define MyVersion "0.0"
#endif

[Setup]
AppId={{1B5DDA69-15AB-44B8-AA81-C0BEAAFA9EB6}
AppName=PC Cleaner
AppVersion={#MyVersion}
AppPublisher=Felo
DefaultDirName={autopf}\PC Cleaner
DefaultGroupName=PC Cleaner
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=PC_Cleaner_Setup
SetupIconFile=logo.ico
UninstallDisplayIcon={app}\pc_cleaner.exe
UninstallDisplayName=PC Cleaner
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\pc_cleaner.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\PC Cleaner"; Filename: "{app}\pc_cleaner.exe"
Name: "{autodesktop}\PC Cleaner"; Filename: "{app}\pc_cleaner.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\pc_cleaner.exe"; Description: "{cm:LaunchProgram,PC Cleaner}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\PCCleaner');
    if DirExists(DataDir) then
      if MsgBox('Also delete PC Cleaner settings and the "Saved deletions" folder? Files inside it will be lost for good.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
