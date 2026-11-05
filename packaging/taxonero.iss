; Installeur Windows du lanceur de bureau (Inno Setup 6) : menu Démarrer, icône sur le bureau au choix, désinstallation.
; Installation pour l'utilisateur, sans droits d'administrateur (au choix : pour tous les utilisateurs).
;   set TAXONERO_VERSION=0.1.0 & set TAXONERO_ARCH=x64 & ISCC packaging\taxonero.iss
; TAXONERO_ARCH : x64 ou arm64. Le lanceur est attendu dans dist\taxonero.exe, l'installeur sort dans out\.
; Les données (modèles, PyTorch, base : plusieurs Go dans %LOCALAPPDATA%\taxonero) restent après la désinstallation.

#define AppVersion GetEnv("TAXONERO_VERSION")
#define Arch GetEnv("TAXONERO_ARCH")

[Setup]
AppId={{868A4DEA-4405-45BD-B253-2414D0A0B30B}
AppName=Taxonero
AppVersion={#AppVersion}
AppPublisher=adn-dan
AppPublisherURL=https://github.com/adn-dan/taxonero
AppSupportURL=https://github.com/adn-dan/taxonero/issues
DefaultDirName={autopf}\Taxonero
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
#if Arch == "arm64"
ArchitecturesAllowed=arm64
ArchitecturesInstallIn64BitMode=arm64
#else
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#endif
OutputDir=..\out
OutputBaseFilename=Taxonero-{#AppVersion}-windows-{#Arch}-setup
SetupIconFile=icons\taxonero.ico
UninstallDisplayIcon={app}\taxonero.exe
WizardStyle=modern
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\taxonero.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Taxonero"; Filename: "{app}\taxonero.exe"
Name: "{autodesktop}\Taxonero"; Filename: "{app}\taxonero.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\taxonero.exe"; Description: "{cm:LaunchProgram,Taxonero}"; Flags: nowait postinstall skipifsilent
