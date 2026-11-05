package com.artradar.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ClipData;
import android.content.ComponentName;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.net.URLEncoder;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER = 1001;
    private static final int NOTIFY_PERM = 1002;
    private WebView web;
    private SharedPreferences sp;
    private ValueCallback<Uri[]> fileCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        sp = getSharedPreferences("artradar", MODE_PRIVATE);
        String extraServer = getIntent().getStringExtra("server");
        if (extraServer != null && !extraServer.trim().isEmpty()) {
            String raw = extraServer.trim();
            if (!raw.startsWith("http://") && !raw.startsWith("https://")) raw = "http://" + raw;
            Uri u = Uri.parse(raw);
            String tok = getIntent().getStringExtra("token");
            if (tok == null || tok.isEmpty()) tok = u.getQueryParameter("token");
            sp.edit().putString("server", u.getScheme() + "://" + u.getAuthority())
                    .putString("token", tok == null ? "" : tok).apply();
        }
        getWindow().setStatusBarColor(Color.parseColor("#171311"));
        getWindow().setNavigationBarColor(Color.parseColor("#171311"));

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.parseColor("#171311"));

        web = new WebView(this);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);
        web.setBackgroundColor(Color.parseColor("#171311"));
        web.addJavascriptInterface(new Object() {
            @JavascriptInterface
            public void openSettings() {
                runOnUiThread(() -> showSettings(false));
            }
        }, "AndroidBridge");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleAppLink(request.getUrl().toString());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return handleAppLink(url);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    view.loadDataWithBaseURL(null,
                            "<html><body style='background:#171311;color:#f3ece3;font-family:sans-serif;padding:32px'>" +
                                    "<h2>连不上 PC 端</h2><p>确认手机和电脑在同一 Wi-Fi，ArtRadar 正在运行，" +
                                    "然后点下面按钮填入电脑管理界面「手机端」显示的新地址。</p>" +
                                    "<p><a href='artradar://settings' style='color:#e07a5f'>打开设置</a>　" +
                                    "<a href='artradar://retry' style='color:#e07a5f'>重试</a></p></body></html>",
                            "text/html", "utf-8", null);
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                fileCallback = callback;
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("image/*");
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                try {
                    startActivityForResult(intent, FILE_CHOOSER);
                    return true;
                } catch (Exception e) {
                    fileCallback = null;
                    return false;
                }
            }
        });

        LinearLayout.LayoutParams webLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        root.addView(web, webLp);
        setContentView(root);

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFY_PERM);
        }

        if (sp.getString("server", "").isEmpty()) {
            showSettings(true);
        } else {
            loadUrl();
        }
        scheduleJob();
    }

    private void loadUrl() {
        String base = sp.getString("server", "").trim();
        String token = sp.getString("token", "").trim();
        if (base.isEmpty()) {
            showSettings(true);
            return;
        }
        if (base.endsWith("/")) base = base.substring(0, base.length() - 1);
        String url = base;
        if (!token.isEmpty()) {
            try {
                url += "?token=" + URLEncoder.encode(token, "UTF-8");
            } catch (Exception ignored) {
            }
        }
        web.loadUrl(url);
    }

    private boolean handleAppLink(String url) {
        if (url.startsWith("artradar://settings")) {
            showSettings(false);
            return true;
        }
        if (url.startsWith("artradar://retry")) {
            loadUrl();
            return true;
        }
        return false;
    }

    private void showSettings(boolean firstRun) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(20), dp(8), dp(20), dp(4));

        TextView hint = new TextView(this);
        hint.setText("在电脑网页顶部点「手机端」，复制配对地址粘贴到下面；也可以只填服务器地址和令牌。");
        hint.setTextSize(13);
        hint.setTextColor(Color.parseColor("#a99a8d"));
        box.addView(hint);

        EditText server = new EditText(this);
        server.setHint("配对地址或 http://192.168.x.x:8765");
        server.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        server.setTextColor(Color.parseColor("#f3ece3"));
        server.setHintTextColor(Color.parseColor("#7f7167"));
        server.setBackgroundTintList(ColorStateList.valueOf(Color.parseColor("#e07a5f")));
        server.setText(sp.getString("server", ""));
        box.addView(server);

        EditText token = new EditText(this);
        token.setHint("令牌 token（地址里带 token 时可留空）");
        token.setInputType(InputType.TYPE_CLASS_TEXT);
        token.setTextColor(Color.parseColor("#f3ece3"));
        token.setHintTextColor(Color.parseColor("#7f7167"));
        token.setBackgroundTintList(ColorStateList.valueOf(Color.parseColor("#e07a5f")));
        token.setText(sp.getString("token", ""));
        box.addView(token);

        AlertDialog.Builder builder = new AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle("连接电脑端 ArtRadar")
                .setView(box)
                .setPositiveButton("保存并连接", null);
        if (firstRun) {
            builder.setNegativeButton("退出", (d, w) -> finish());
        } else {
            builder.setNegativeButton("取消", null);
        }
        AlertDialog dialog = builder.create();
        dialog.setCancelable(!firstRun);
        dialog.setOnShowListener(d -> {
            if (dialog.getWindow() != null) {
                dialog.getWindow().setBackgroundDrawable(
                        new ColorDrawable(Color.parseColor("#211b18")));
            }
            dialog.getButton(AlertDialog.BUTTON_POSITIVE)
                    .setTextColor(Color.parseColor("#e07a5f"));
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE)
                    .setTextColor(Color.parseColor("#a99a8d"));
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
                String raw = server.getText().toString().trim();
                String tok = token.getText().toString().trim();
                if (raw.isEmpty()) {
                    Toast.makeText(this, "请填地址", Toast.LENGTH_SHORT).show();
                    return;
                }
                if (!raw.startsWith("http://") && !raw.startsWith("https://")) {
                    raw = "http://" + raw;
                }
                Uri uri = Uri.parse(raw);
                if (tok.isEmpty()) {
                    String q = uri.getQueryParameter("token");
                    if (q != null) tok = q;
                }
                String base = uri.getScheme() + "://" + uri.getAuthority();
                sp.edit().putString("server", base).putString("token", tok).apply();
                dialog.dismiss();
                loadUrl();
                scheduleJob();
            });
        });
        dialog.show();
    }

    private void scheduleJob() {
        JobScheduler js = (JobScheduler) getSystemService(JOB_SCHEDULER_SERVICE);
        JobInfo job = new JobInfo.Builder(7, new ComponentName(this, ArtRadarJobService.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(15 * 60 * 1000L)
                .setPersisted(true)
                .build();
        try {
            js.schedule(job);
        } catch (Exception ignored) {
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER || fileCallback == null) return;
        Uri[] results = null;
        if (resultCode == RESULT_OK && data != null) {
            ClipData clip = data.getClipData();
            if (clip != null) {
                results = new Uri[clip.getItemCount()];
                for (int i = 0; i < clip.getItemCount(); i++) {
                    results[i] = clip.getItemAt(i).getUri();
                }
            } else if (data.getData() != null) {
                results = new Uri[]{data.getData()};
            }
        }
        fileCallback.onReceiveValue(results);
        fileCallback = null;
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }
}
