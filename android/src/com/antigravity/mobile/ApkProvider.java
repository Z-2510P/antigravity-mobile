package com.antigravity.mobile;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

public class ApkProvider extends ContentProvider {
    public static final String AUTHORITY = "com.antigravity.mobile.apkprovider";

    private File getTargetApkFile() {
        if (getContext() == null) return null;
        return new File(new File(getContext().getCacheDir(), "apk"), "AntigravityMobile.apk");
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File apkFile = getTargetApkFile();
        if (apkFile == null || !apkFile.exists()) {
            throw new FileNotFoundException("APK file not found in cache");
        }
        return ParcelFileDescriptor.open(apkFile, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        File apkFile = getTargetApkFile();
        if (projection == null) {
            projection = new String[]{ OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE };
        }
        String[] cols = new String[projection.length];
        Object[] values = new Object[projection.length];
        int i = 0;
        for (String col : projection) {
            if (OpenableColumns.DISPLAY_NAME.equals(col)) {
                cols[i] = OpenableColumns.DISPLAY_NAME;
                values[i] = "AntigravityMobile.apk";
                i++;
            } else if (OpenableColumns.SIZE.equals(col)) {
                cols[i] = OpenableColumns.SIZE;
                values[i] = (apkFile != null && apkFile.exists()) ? apkFile.length() : 0L;
                i++;
            }
        }
        String[] finalCols = new String[i];
        Object[] finalVals = new Object[i];
        System.arraycopy(cols, 0, finalCols, 0, i);
        System.arraycopy(values, 0, finalVals, 0, i);
        MatrixCursor cursor = new MatrixCursor(finalCols, 1);
        cursor.addRow(finalVals);
        return cursor;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
