package party.beetroot.hooktest;
import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.callbacks.XC_LoadPackage.LoadPackageParam;

public class Init implements IXposedHookLoadPackage {
    static final class Cb extends XC_MethodHook {
        private final String pkg; private final String where;
        Cb(String pkg, String where) { this.pkg = pkg; this.where = where; }
        protected void afterHookedMethod(MethodHookParam param) {
            XposedBridge.log("BEETROOT_HOOK_FIRED " + where + " pkg=" + pkg);
        }
    }
    public void handleLoadPackage(final LoadPackageParam lpparam) {
        XposedBridge.log("BEETROOT_HOOK_LOADED pkg=" + lpparam.packageName);
        try {
            Object u1 = XposedHelpers.findAndHookMethod("android.app.Activity", lpparam.classLoader, "onResume", new Cb(lpparam.packageName, "onResume"));
            Object u2 = XposedHelpers.findAndHookMethod("android.app.Activity", lpparam.classLoader, "onCreate", android.os.Bundle.class, new Cb(lpparam.packageName, "onCreate"));
            XposedBridge.log("BEETROOT_HOOK_INSTALLED onResume=" + (u1 != null) + " onCreate=" + (u2 != null) + " pkg=" + lpparam.packageName);
        } catch (Throwable t) {
            XposedBridge.log("BEETROOT_HOOK_SETUP_FAILED " + t);
        }
    }
}
