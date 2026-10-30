"""Build the local notification adapter with the already installed Xcode toolchain."""
from pathlib import Path
import plistlib,subprocess
ROOT=Path(__file__).resolve().parents[1]
app=Path.home()/'Applications/海辛通知.app'
mac=app/'Contents/MacOS';mac.mkdir(parents=True,exist_ok=True)
info={'CFBundleExecutable':'HaixinNotifications','CFBundleIdentifier':'cn.haixin.local.notifications','CFBundleName':'海辛','CFBundleDisplayName':'海辛','CFBundlePackageType':'APPL','CFBundleVersion':'1','CFBundleShortVersionString':'1.0','LSUIElement':True,'NSHighResolutionCapable':True,'LSMinimumSystemVersion':'12.0'}
(app/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
subprocess.run(['xcrun','swiftc','-O',str(ROOT/'native/HaixinNotifications.swift'),'-o',str(mac/'HaixinNotifications'),'-framework','Cocoa','-framework','UserNotifications'],check=True)
subprocess.run(['codesign','--force','--sign','-',str(app)],check=True,capture_output=True)
subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister','-f',str(app)],check=True,capture_output=True)
print('本机海辛通知组件已构建。')
