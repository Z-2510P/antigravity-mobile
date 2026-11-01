package com.antigravity.mobile;

import android.app.Activity;
import android.app.DownloadManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ClipData;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.provider.MediaStore;
import android.provider.Settings;
import android.util.Base64;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.SslErrorHandler;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSession;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

public class MainActivity extends Activity {
    private static final String PREFS_NAME = "antigravity_prefs";
    private static final String KEY_SERVER_URL = "server_url_v3";
    private static final String CHANNEL_ID = "antigravity_sync_channel";

    private WebView webView;
    private SharedPreferences prefs;
    private ValueCallback<Uri[]> filePathCallback;
    private static final int FILE_CHOOSER_RESULT_CODE = 2002;
    private static final int INSTALL_PERMISSION_RESULT_CODE = 2003;
    private int notifId = 1001;

    private String getBuiltInDefaultUrl() {
        try {
            String resUrl = getString(R.string.default_server_url);
            if (resUrl != null && !resUrl.trim().isEmpty()) {
                return resUrl.trim();
            }
        } catch (Exception ignored) {
        }
        return "http://127.0.0.1:8899";
    }

    private String resolveTargetServerUrl() {
        String defUrl = getBuiltInDefaultUrl();
        String raw = prefs.getString(KEY_SERVER_URL, defUrl);
        if (raw == null || raw.trim().isEmpty()) {
            prefs.edit().putString(KEY_SERVER_URL, defUrl).apply();
            return defUrl;
        }
        return raw.trim();
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Window window = getWindow();
        window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
        int bgLight = Color.parseColor("#F7F7F7");
        window.setStatusBarColor(bgLight);
        window.setNavigationBarColor(bgLight);

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        createNotificationChannel();

        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 101);
        }

        webView = new WebView(this);
        webView.setBackgroundColor(bgLight);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setAllowFileAccessFromFileURLs(true);
        settings.setAllowUniversalAccessFromFileURLs(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setTextZoom(100);

        webView.addJavascriptInterface(new AndroidBridge(), "AndroidBridge");

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.proceed();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    String failingUrl = request.getUrl() != null ? request.getUrl().toString() : "";
                    if (!failingUrl.startsWith("file://")) {
                        view.loadUrl("file:///android_asset/index.html");
                        return;
                    }
                }
                super.onReceivedError(view, request, error);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                String targetHost = "";
                try {
                    Uri tUri = Uri.parse(resolveTargetServerUrl());
                    if (tUri.getHost() != null) targetHost = tUri.getHost();
                } catch (Exception ignored) {}
                if (url.startsWith("file://") || (!targetHost.isEmpty() && url.contains(targetHost)) || url.contains("127.0.0.1") || url.contains("localhost")) {
                    return false;
                }
                try {
                    Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
                    startActivity(intent);
                } catch (Exception ignored) {
                }
                return true;
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> filePathCallback, FileChooserParams fileChooserParams) {
                if (MainActivity.this.filePathCallback != null) {
                    MainActivity.this.filePathCallback.onReceiveValue(null);
                }
                MainActivity.this.filePathCallback = filePathCallback;
                try {
                    // 1. File System / Documents picker supporting images, videos, and all files
                    Intent contentSelectionIntent = new Intent(Intent.ACTION_GET_CONTENT);
                    contentSelectionIntent.addCategory(Intent.CATEGORY_OPENABLE);
                    contentSelectionIntent.setType("*/*");
                    contentSelectionIntent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"image/*", "video/*", "*/*"});
                    contentSelectionIntent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                    contentSelectionIntent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);

                    // 2. Direct Photo & Video Album (Gallery) picker
                    Intent galleryIntent = new Intent(Intent.ACTION_PICK, MediaStore.Images.Media.EXTERNAL_CONTENT_URI);
                    galleryIntent.setType("image/* video/*");
                    galleryIntent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"image/*", "video/*"});
                    galleryIntent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                    galleryIntent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);

                    Intent chooserIntent = Intent.createChooser(contentSelectionIntent, "Select Photos, Videos, or Files");
                    chooserIntent.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[]{ galleryIntent });
                    startActivityForResult(chooserIntent, FILE_CHOOSER_RESULT_CODE);
                } catch (Exception e) {
                    try {
                        Intent fallback = fileChooserParams.createIntent();
                        startActivityForResult(fallback, FILE_CHOOSER_RESULT_CODE);
                    } catch (Exception ex) {
                        MainActivity.this.filePathCallback = null;
                        return false;
                    }
                }
                return true;
            }
        });

        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimeType, long contentLength) {
                if (url == null || url.startsWith("blob:")) {
                    return;
                }
                String guessedName = URLUtil.guessFileName(url, contentDisposition, mimeType);
                if (guessedName == null || guessedName.trim().isEmpty()) {
                    guessedName = "Antigravity_Download";
                }
                try {
                    DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                    req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, guessedName);
                    DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                    if (dm != null) {
                        dm.enqueue(req);
                        Toast.makeText(MainActivity.this, "Downloading " + guessedName + "...", Toast.LENGTH_SHORT).show();
                        return;
                    }
                } catch (Exception ignored) {
                }
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
                    startActivity(i);
                } catch (Exception ignored) {
                }
            }
        });

        setContentView(webView);
        webView.loadUrl(resolveTargetServerUrl());
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "Antigravity Task Sync",
                NotificationManager.IMPORTANCE_HIGH
            );
            channel.setDescription("Notifications when Antigravity completes a conversation turn or file download");
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.createNotificationChannel(channel);
            }
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == FILE_CHOOSER_RESULT_CODE) {
            if (filePathCallback == null) return;
            Uri[] results = null;
            if (resultCode == Activity.RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    int count = data.getClipData().getItemCount();
                    results = new Uri[count];
                    for (int i = 0; i < count; i++) {
                        results[i] = data.getClipData().getItemAt(i).getUri();
                    }
                } else if (data.getData() != null) {
                    results = new Uri[]{ data.getData() };
                } else {
                    results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
                }
            }
            filePathCallback.onReceiveValue(results);
            filePathCallback = null;
        } else if (requestCode == INSTALL_PERMISSION_RESULT_CODE) {
            launchPendingApkInstaller();
        } else {
            super.onActivityResult(requestCode, resultCode, data);
        }
    }

    private void launchPendingApkInstaller() {
        try {
            File apkFile = new File(new File(getCacheDir(), "apk"), "AntigravityMobile.apk");
            if (!apkFile.exists() || apkFile.length() < 1000) {
                Toast.makeText(MainActivity.this, "Update package not found", Toast.LENGTH_SHORT).show();
                return;
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                if (!getPackageManager().canRequestPackageInstalls()) {
                    Toast.makeText(MainActivity.this, "请开启一次「允许安装未知应用」权限以完成应用内更新", Toast.LENGTH_LONG).show();
                    Intent permIntent = new Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:" + getPackageName())
                    );
                    startActivityForResult(permIntent, INSTALL_PERMISSION_RESULT_CODE);
                    return;
                }
            }
            Uri apkUri = Uri.parse("content://" + ApkProvider.AUTHORITY + "/AntigravityMobile.apk");
            Intent installIntent = new Intent(Intent.ACTION_VIEW);
            installIntent.setDataAndType(apkUri, "application/vnd.android.package-archive");
            installIntent.setClipData(ClipData.newRawUri("", apkUri));
            installIntent.putExtra(Intent.EXTRA_NOT_UNKNOWN_SOURCE, true);
            installIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            installIntent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivity(installIntent);
        } catch (Exception e) {
            Toast.makeText(MainActivity.this, "Installer error: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null) {
            webView.evaluateJavascript(
                "(function(){ return !!(window.handleAndroidBack && window.handleAndroidBack()); })()",
                new ValueCallback<String>() {
                    @Override
                    public void onReceiveValue(String value) {
                        if (!"true".equals(value)) {
                            if (webView.canGoBack()) {
                                webView.goBack();
                            } else {
                                MainActivity.super.onBackPressed();
                            }
                        }
                    }
                }
            );
        } else {
            super.onBackPressed();
        }
    }

    public class AndroidBridge {
        @JavascriptInterface
        public String getServerUrl() {
            return resolveTargetServerUrl();
        }

        @JavascriptInterface
        public void setServerUrl(String url) {
            if (url != null && !url.trim().isEmpty()) {
                prefs.edit().putString(KEY_SERVER_URL, url.trim()).apply();
            }
        }

        @JavascriptInterface
        public int getAppVersionCode() {
            try {
                PackageInfo pInfo = getPackageManager().getPackageInfo(getPackageName(), 0);
                return pInfo.versionCode;
            } catch (Exception e) {
                return 1;
            }
        }

        @JavascriptInterface
        public String getAppVersionName() {
            try {
                PackageInfo pInfo = getPackageManager().getPackageInfo(getPackageName(), 0);
                return pInfo.versionName != null ? pInfo.versionName : "2.0.0";
            } catch (Exception e) {
                return "2.0.0";
            }
        }

        @JavascriptInterface
        public void setSystemBarTheme(final boolean isDark) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    try {
                        Window window = getWindow();
                        int bg = Color.parseColor(isDark ? "#161618" : "#F7F7F7");
                        window.setStatusBarColor(bg);
                        window.setNavigationBarColor(bg);
                        if (webView != null) {
                            webView.setBackgroundColor(bg);
                        }
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                            View decor = window.getDecorView();
                            int flags = decor.getSystemUiVisibility();
                            if (isDark) {
                                flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                                    flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                                }
                            } else {
                                flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                                    flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                                }
                            }
                            decor.setSystemUiVisibility(flags);
                        }
                    } catch (Exception ignored) {
                    }
                }
            });
        }

        @JavascriptInterface
        public boolean downloadFileFromUrl(final String fileUrl, final String fileName, final String mimeType) {
            if (fileUrl == null || fileUrl.trim().isEmpty()) return false;
            final String safeName = (fileName != null && !fileName.trim().isEmpty()) ? fileName.trim() : "download.bin";
            final String safeMime = (mimeType != null && !mimeType.trim().isEmpty()) ? mimeType.trim() : "application/octet-stream";
            new Thread(new Runnable() {
                @Override
                public void run() {
                    try {
                        URL u = new URL(fileUrl.trim());
                        HttpURLConnection conn = (HttpURLConnection) u.openConnection();
                        if (conn instanceof HttpsURLConnection) {
                            HttpsURLConnection https = (HttpsURLConnection) conn;
                            TrustManager[] trustAll = new TrustManager[]{
                                new X509TrustManager() {
                                    @Override public void checkClientTrusted(X509Certificate[] chain, String authType) {}
                                    @Override public void checkServerTrusted(X509Certificate[] chain, String authType) {}
                                    @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
                                }
                            };
                            SSLContext sc = SSLContext.getInstance("TLS");
                            sc.init(null, trustAll, new SecureRandom());
                            https.setSSLSocketFactory(sc.getSocketFactory());
                            https.setHostnameVerifier(new HostnameVerifier() {
                                @Override public boolean verify(String hostname, SSLSession session) { return true; }
                            });
                        }
                        conn.setConnectTimeout(15000);
                        conn.setReadTimeout(120000);
                        conn.connect();
                        if (conn.getResponseCode() >= 400) {
                            throw new Exception("HTTP " + conn.getResponseCode());
                        }
                        final boolean isApk = safeName.toLowerCase().endsWith(".apk");
                        File cachedApk = null;
                        FileOutputStream apkFos = null;
                        if (isApk) {
                            File apkDir = new File(getCacheDir(), "apk");
                            if (!apkDir.exists()) apkDir.mkdirs();
                            cachedApk = new File(apkDir, "AntigravityMobile.apk");
                            apkFos = new FileOutputStream(cachedApk, false);
                        }
                        OutputStream pubOs = null;
                        if (Build.VERSION.SDK_INT >= 29) {
                            ContentValues values = new ContentValues();
                            values.put(MediaStore.Downloads.DISPLAY_NAME, safeName);
                            values.put(MediaStore.Downloads.MIME_TYPE, safeMime);
                            values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                            Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                            if (uri != null) {
                                pubOs = getContentResolver().openOutputStream(uri);
                            }
                        } else {
                            File dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                            if (!dir.exists()) dir.mkdirs();
                            pubOs = new FileOutputStream(new File(dir, safeName));
                        }
                        try (InputStream is = conn.getInputStream()) {
                            byte[] buf = new byte[65536];
                            int n;
                            while ((n = is.read(buf)) != -1) {
                                if (pubOs != null) pubOs.write(buf, 0, n);
                                if (apkFos != null) apkFos.write(buf, 0, n);
                            }
                        } finally {
                            if (pubOs != null) {
                                try { pubOs.flush(); pubOs.close(); } catch (Exception ignored) {}
                            }
                            if (apkFos != null) {
                                try { apkFos.flush(); apkFos.close(); } catch (Exception ignored) {}
                            }
                        }
                        runOnUiThread(new Runnable() {
                            @Override
                            public void run() {
                                Toast.makeText(MainActivity.this, "Saved to Downloads: " + safeName, Toast.LENGTH_LONG).show();
                                if (isApk) {
                                    launchPendingApkInstaller();
                                }
                            }
                        });
                        showNotification("Download Complete", "Saved to Downloads/" + safeName);
                    } catch (final Exception e) {
                        runOnUiThread(new Runnable() {
                            @Override
                            public void run() {
                                Toast.makeText(MainActivity.this, "Download failed: " + e.getMessage(), Toast.LENGTH_LONG).show();
                            }
                        });
                    }
                }
            }).start();
            return true;
        }

        @JavascriptInterface
        public boolean installApkBase64(String base64Data) {
            try {
                if (base64Data == null || base64Data.isEmpty()) return false;
                String cleanB64 = base64Data;
                int commaIdx = cleanB64.indexOf(',');
                if (commaIdx >= 0) {
                    cleanB64 = cleanB64.substring(commaIdx + 1);
                }
                byte[] bytes = Base64.decode(cleanB64, Base64.DEFAULT);
                if (bytes == null || bytes.length < 1000 || bytes[0] != 'P' || bytes[1] != 'K') {
                    return false;
                }
                File apkDir = new File(getCacheDir(), "apk");
                if (!apkDir.exists()) apkDir.mkdirs();
                File apkFile = new File(apkDir, "AntigravityMobile.apk");
                try (FileOutputStream fos = new FileOutputStream(apkFile, false)) {
                    fos.write(bytes);
                    fos.flush();
                }
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        launchPendingApkInstaller();
                    }
                });
                return true;
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public boolean saveBase64File(String base64Data, String fileName, String mimeType) {
            try {
                String cleanB64 = base64Data;
                int commaIdx = cleanB64.indexOf(',');
                if (commaIdx >= 0) {
                    cleanB64 = cleanB64.substring(commaIdx + 1);
                }
                byte[] bytes = Base64.decode(cleanB64, Base64.DEFAULT);
                String safeName = (fileName != null && !fileName.trim().isEmpty()) ? fileName.trim() : "download.bin";
                String safeMime = (mimeType != null && !mimeType.trim().isEmpty()) ? mimeType.trim() : "application/octet-stream";

                if (Build.VERSION.SDK_INT >= 29) {
                    ContentValues values = new ContentValues();
                    values.put(MediaStore.Downloads.DISPLAY_NAME, safeName);
                    values.put(MediaStore.Downloads.MIME_TYPE, safeMime);
                    values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                    ContentResolver resolver = getContentResolver();
                    Uri uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                    if (uri != null) {
                        try (OutputStream os = resolver.openOutputStream(uri)) {
                            if (os != null) {
                                os.write(bytes);
                                os.flush();
                            }
                        }
                    }
                } else {
                    File dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                    if (!dir.exists()) dir.mkdirs();
                    File outFile = new File(dir, safeName);
                    try (FileOutputStream fos = new FileOutputStream(outFile)) {
                        fos.write(bytes);
                        fos.flush();
                    }
                }
                final String toastName = safeName;
                final boolean isApkFile = safeName.toLowerCase().endsWith(".apk");
                if (isApkFile && bytes.length > 1000 && bytes[0] == 'P' && bytes[1] == 'K') {
                    File apkDir = new File(getCacheDir(), "apk");
                    if (!apkDir.exists()) apkDir.mkdirs();
                    File cachedApk = new File(apkDir, "AntigravityMobile.apk");
                    try (FileOutputStream fos = new FileOutputStream(cachedApk, false)) {
                        fos.write(bytes);
                        fos.flush();
                    }
                }
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        Toast.makeText(MainActivity.this, "Saved to Downloads: " + toastName, Toast.LENGTH_LONG).show();
                        if (isApkFile) {
                            launchPendingApkInstaller();
                        }
                    }
                });
                showNotification("Download Complete", "Saved to Downloads/" + safeName);
                return true;
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public void vibrate(int durationMs) {
            try {
                Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
                if (v != null && v.hasVibrator()) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        v.vibrate(VibrationEffect.createOneShot(durationMs, VibrationEffect.DEFAULT_AMPLITUDE));
                    } else {
                        v.vibrate(durationMs);
                    }
                }
            } catch (Exception ignored) {
            }
        }

        @JavascriptInterface
        public void showNotification(String title, String body) {
            try {
                NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
                if (nm == null) return;

                Intent intent = new Intent(MainActivity.this, MainActivity.class);
                intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                int flags = PendingIntent.FLAG_UPDATE_CURRENT;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    flags |= PendingIntent.FLAG_IMMUTABLE;
                }
                PendingIntent pi = PendingIntent.getActivity(MainActivity.this, 0, intent, flags);

                Notification.Builder builder;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    builder = new Notification.Builder(MainActivity.this, CHANNEL_ID);
                } else {
                    builder = new Notification.Builder(MainActivity.this);
                }
                builder.setSmallIcon(R.drawable.ic_launcher)
                       .setContentTitle(title != null ? title : "Antigravity")
                       .setContentText(body != null ? body : "Task finished")
                       .setAutoCancel(true)
                       .setContentIntent(pi);

                nm.notify(notifId++, builder.build());
            } catch (Exception ignored) {
            }
        }
    }
}
