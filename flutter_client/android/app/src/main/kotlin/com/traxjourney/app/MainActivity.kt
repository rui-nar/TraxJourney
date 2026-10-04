package com.traxjourney.app

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.ByteArrayOutputStream
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Receives a .gpx another app opens ("Open with", ACTION_VIEW) or shares
 * (ACTION_SEND) with TraxJourney (issue #368).
 *
 * The file is held here until Dart asks for it on [CHANNEL] (`take`), and Dart
 * is told when one arrives while the app runs (`received`). See
 * lib/src/projects/incoming_gpx.dart for the rest of the flow.
 *
 * A shared file is untrusted input from any app: any size, any type. Its size
 * is checked before it is opened, and the read stops at the cap Dart passes
 * even when the provider's reported size is wrong. The read runs off the main
 * thread.
 *
 * `flutter_deeplinking_enabled` hands an ACTION_VIEW's data to the router as
 * its first route; a `content://` file is not a route, so the data is taken
 * off the intent before Flutter sees it. App Links (https) are left untouched.
 */
class MainActivity : FlutterActivity() {
    private var channel: MethodChannel? = null
    private var pendingFile: Uri? = null
    private val reader: ExecutorService = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        // A recreated activity, or one relaunched from Recents, is handed its
        // original intent again: that file was already taken the first time.
        val fresh = savedInstanceState == null &&
            (intent.flags and Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY) == 0
        intent = takeIncomingFile(intent, hold = fresh)
        super.onCreate(savedInstanceState)
    }

    override fun onNewIntent(intent: Intent) {
        val before = pendingFile
        val forFlutter = takeIncomingFile(intent, hold = true)
        super.onNewIntent(forFlutter)
        if (pendingFile != null && pendingFile !== before) {
            channel?.invokeMethod("received", null)
        }
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL).also {
            it.setMethodCallHandler { call, result ->
                when (call.method) {
                    "take" -> take(call.argument<Number>("maxBytes")!!.toLong(), result)
                    else -> result.notImplemented()
                }
            }
        }
    }

    override fun onDestroy() {
        reader.shutdown()
        super.onDestroy()
    }

    /**
     * Holds the file [intent] carries (when [hold]) and returns the intent
     * Flutter should see: without its data when that data is a file.
     */
    private fun takeIncomingFile(intent: Intent, hold: Boolean): Intent {
        val file: Uri? = when (intent.action) {
            Intent.ACTION_VIEW -> intent.data?.takeIf {
                it.scheme == "content" || it.scheme == "file"
            }
            Intent.ACTION_SEND -> sharedStream(intent)
            else -> null
        }
        if (file == null) return intent
        if (hold) pendingFile = file
        // A share carries its file as an extra, which Flutter never routes.
        if (intent.action != Intent.ACTION_VIEW) return intent
        return Intent(intent).apply { data = null }
    }

    @Suppress("DEPRECATION")
    private fun sharedStream(intent: Intent): Uri? =
        if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        } else {
            intent.getParcelableExtra(Intent.EXTRA_STREAM)
        }

    /** Answers `take`: the held file's name and bytes, or null when none. */
    private fun take(maxBytes: Long, result: MethodChannel.Result) {
        val file = pendingFile ?: return result.success(null)
        pendingFile = null
        reader.execute {
            val answer = try {
                read(file, maxBytes)
            } catch (e: Exception) {
                mapOf("name" to null, "error" to (e.message ?: e.javaClass.simpleName))
            }
            mainHandler.post { result.success(answer) }
        }
    }

    /**
     * Reads [file] whole when it is at most [maxBytes]. A file over the cap is
     * answered `tooLarge`: refused unopened when the provider says so, and
     * the moment the read passes the cap when it doesn't.
     */
    private fun read(file: Uri, maxBytes: Long): Map<String, Any?> {
        var name: String? = null
        var size: Long? = null
        contentResolver.query(
            file, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
            null, null, null,
        )?.use { cursor ->
            if (cursor.moveToFirst()) {
                val nameAt = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (nameAt >= 0 && !cursor.isNull(nameAt)) name = cursor.getString(nameAt)
                val sizeAt = cursor.getColumnIndex(OpenableColumns.SIZE)
                if (sizeAt >= 0 && !cursor.isNull(sizeAt)) size = cursor.getLong(sizeAt)
            }
        }
        val fileName = name ?: file.lastPathSegment
        if ((size ?: 0L) > maxBytes) return mapOf("name" to fileName, "tooLarge" to true)

        val bytes = contentResolver.openInputStream(file)?.use { input ->
            val out = ByteArrayOutputStream()
            val buffer = ByteArray(64 * 1024)
            var total = 0L
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                total += count
                if (total > maxBytes) return mapOf("name" to fileName, "tooLarge" to true)
                out.write(buffer, 0, count)
            }
            out.toByteArray()
        } ?: return mapOf("name" to fileName, "error" to "no stream")
        return mapOf("name" to fileName, "bytes" to bytes)
    }

    private companion object {
        const val CHANNEL = "com.traxjourney.app/incoming_file"
    }
}
