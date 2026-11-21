// AGP 9 has Kotlin built in, so only the Compose compiler plugin is added on top.
plugins {
    id("com.android.application") version "9.3.3" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.2.10" apply false
}
