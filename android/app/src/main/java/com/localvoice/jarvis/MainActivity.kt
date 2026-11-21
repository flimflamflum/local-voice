package com.localvoice.jarvis

import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import com.localvoice.jarvis.ui.JarvisApp
import com.localvoice.jarvis.ui.JarvisTheme

class MainActivity : ComponentActivity() {
    private val vm: JarvisViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        setContent {
            JarvisTheme { JarvisApp(vm) }
        }
    }

    override fun onStop() {
        super.onStop()
        vm.onBackground() // stop the mic when the app leaves the screen
    }
}
