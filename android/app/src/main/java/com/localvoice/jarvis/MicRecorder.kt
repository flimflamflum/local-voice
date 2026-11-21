package com.localvoice.jarvis

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.SystemClock
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * Records 16 kHz mono 16-bit audio and streams it out frame by frame while you talk.
 * Stops by itself once you go quiet (same energy-based endpointing as the desktop app).
 * Callbacks run on the recording thread.
 */
class MicRecorder {
    enum class Result { Spoke, NoSpeech, Cancelled, Failed }

    interface Listener {
        fun onFrame(data: ByteArray, length: Int)
        fun onLevel(level: Float)
        fun onEnd(result: Result)
    }

    @Volatile private var stopFlag = false
    @Volatile private var cancelFlag = false
    @Volatile var active = false
        private set

    fun start(listener: Listener) {
        if (active) return
        stopFlag = false
        cancelFlag = false
        active = true
        Thread({ run(listener) }, "mic").start()
    }

    /** Finish now and keep what was said. */
    fun stop() {
        stopFlag = true
    }

    /** Abort and throw the audio away. */
    fun cancel() {
        cancelFlag = true
        stopFlag = true
    }

    @SuppressLint("MissingPermission") // the UI asks for RECORD_AUDIO before starting
    private fun run(l: Listener) {
        val minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                max(minBuf, BLOCK * 2 * 8),
            )
        } catch (e: Exception) {
            null
        }
        if (rec == null || rec.state != AudioRecord.STATE_INITIALIZED) {
            rec?.release()
            active = false
            l.onEnd(Result.Failed)
            return
        }

        val shorts = ShortArray(BLOCK)
        val bytes = ByteArray(BLOCK * 2)
        var result = Result.Spoke
        var noiseFloor = -1.0
        val calibration = ArrayList<Double>()
        var speechStarted = false
        val t0 = SystemClock.elapsedRealtime()
        var lastVoice = t0

        try {
            rec.startRecording()
            while (!stopFlag) {
                val n = rec.read(shorts, 0, BLOCK)
                if (n < 0) {
                    result = Result.Failed
                    break
                }
                if (n == 0) continue
                var sum = 0.0
                for (i in 0 until n) {
                    val v = shorts[i].toInt()
                    sum += (v * v).toDouble()
                    bytes[2 * i] = (v and 0xff).toByte()
                    bytes[2 * i + 1] = ((v shr 8) and 0xff).toByte()
                }
                l.onFrame(bytes, n * 2)

                val rms = sqrt(sum / n) / 32768.0
                val now = SystemClock.elapsedRealtime()
                if (noiseFloor < 0) {
                    calibration += rms
                    if (calibration.size >= 8) noiseFloor = calibration.sorted()[calibration.size / 2] // ~250 ms of room tone
                } else {
                    val threshold = max(0.012, noiseFloor * 3.0)
                    if (rms > threshold) {
                        speechStarted = true
                        lastVoice = now
                    } else if (speechStarted && now - lastVoice > SILENCE_AFTER_MS) {
                        break
                    } else if (!speechStarted) {
                        noiseFloor = 0.95 * noiseFloor + 0.05 * rms
                        if (now - t0 > NO_SPEECH_TIMEOUT_MS) {
                            result = Result.NoSpeech
                            break
                        }
                    }
                }
                if (now - t0 > MAX_MS) break
                l.onLevel(min(1.0, rms * 12).toFloat())
            }
        } catch (e: Exception) {
            result = Result.Failed
        } finally {
            try {
                rec.stop()
            } catch (_: Exception) {
            }
            rec.release()
        }
        if (cancelFlag) result = Result.Cancelled
        active = false
        l.onLevel(0f)
        l.onEnd(result)
    }

    companion object {
        const val RATE = 16000
        private const val BLOCK = 512 // 32 ms
        private const val SILENCE_AFTER_MS = 1100
        private const val NO_SPEECH_TIMEOUT_MS = 8000
        private const val MAX_MS = 60_000
    }
}
