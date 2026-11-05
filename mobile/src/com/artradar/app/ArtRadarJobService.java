package com.artradar.app;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.job.JobParameters;
import android.app.job.JobService;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.Locale;

public class ArtRadarJobService extends JobService {
    private static final String CHANNEL = "artradar_pending";

    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> {
            try {
                check();
            } catch (Exception ignored) {
            }
            jobFinished(params, false);
        }).start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return false;
    }

    private void check() throws Exception {
        SharedPreferences sp = getSharedPreferences("artradar", MODE_PRIVATE);
        String base = sp.getString("server", "").trim();
        String token = sp.getString("token", "").trim();
        if (base.isEmpty()) return;
        if (base.endsWith("/")) base = base.substring(0, base.length() - 1);
        URL url = new URL(base + "/api/state");
        HttpURLConnection c = (HttpURLConnection) url.openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(8000);
        c.setRequestProperty("X-Art-Token", token);
        if (c.getResponseCode() != 200) return;
        StringBuilder sb = new StringBuilder();
        try (BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream()))) {
            String line;
            while ((line = br.readLine()) != null) sb.append(line);
        }
        JSONObject data = new JSONObject(sb.toString());
        int pending = data.optJSONObject("counts") != null
                ? data.getJSONObject("counts").optInt("pending", 0) : 0;
        int last = sp.getInt("last_pending", -1);
        Calendar cal = Calendar.getInstance();
        String today = new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(cal.getTime());
        boolean daily = cal.get(Calendar.HOUR_OF_DAY) >= 9
                && !today.equals(sp.getString("last_day", ""));
        if ((pending > last && pending > 0) || (daily && pending > 0)) {
            notifyPending(pending, daily);
        }
        SharedPreferences.Editor ed = sp.edit().putInt("last_pending", pending);
        if (daily) ed.putString("last_day", today);
        ed.apply();
    }

    private void notifyPending(int pending, boolean daily) {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            return;
        }
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "ArtRadar 待确认",
                    NotificationManager.IMPORTANCE_DEFAULT);
            nm.createNotificationChannel(ch);
        }
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            b = new Notification.Builder(this, CHANNEL);
        } else {
            b = new Notification.Builder(this);
        }
        b.setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle(daily ? "ArtRadar 今日素材" : "ArtRadar 有新素材")
                .setContentText(pending + " 张待确认，点开处理")
                .setContentIntent(pi)
                .setAutoCancel(true);
        nm.notify(7, b.build());
    }
}
