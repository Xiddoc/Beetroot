package de.robv.android.xposed;
public abstract class XC_MethodHook {
    public static class Unhook {}
    public static class MethodHookParam {}
    protected void beforeHookedMethod(MethodHookParam param) throws Throwable {}
    protected void afterHookedMethod(MethodHookParam param) throws Throwable {}
}
