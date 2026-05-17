import java.util.Properties

plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

val keystorePropertiesFile = rootProject.file("key.properties")
val keystoreProperties = Properties().apply {
    if (keystorePropertiesFile.exists()) {
        keystorePropertiesFile.inputStream().use { load(it) }
    }
}

fun keystoreProperty(name: String): String {
    return keystoreProperties.getProperty(name)?.trim().orEmpty()
}

val releaseStoreFilePath = keystoreProperty("storeFile")
val isFdroidBuild =
    providers
        .gradleProperty("syncwave.fdroid")
        .map { it.equals("true", ignoreCase = true) }
        .getOrElse(false)
val hasReleaseSigningConfig =
    !isFdroidBuild &&
        keystorePropertiesFile.exists() &&
        releaseStoreFilePath.isNotEmpty() &&
        keystoreProperty("storePassword").isNotEmpty() &&
        keystoreProperty("keyAlias").isNotEmpty() &&
        keystoreProperty("keyPassword").isNotEmpty()

android {
    namespace = "io.github.opencodequark.syncwave"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_17.toString()
    }

    defaultConfig {
        applicationId = "io.github.opencodequark.syncwave"
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        create("release") {
            if (hasReleaseSigningConfig) {
                storeFile = rootProject.file(releaseStoreFilePath)
                storePassword = keystoreProperty("storePassword")
                keyAlias = keystoreProperty("keyAlias")
                keyPassword = keystoreProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = if (hasReleaseSigningConfig) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
        }
    }
}

flutter {
    source = "../.."
}
