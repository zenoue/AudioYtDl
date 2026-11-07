plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val ytdlVersion = "0.18.1"
val buildNumber = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()

android {
    namespace = "com.zenoue.audioytdl"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.zenoue.audioytdl"
        minSdk = 24
        targetSdk = 35
        versionCode = buildNumber
        versionName = project.property("appVersion") as String
    }

    // Keystore cố định (chỉ dùng cá nhân) để các bản build sau cài đè được lên bản trước.
    signingConfigs {
        create("fixed") {
            storeFile = file("release.keystore")
            storePassword = "playlistaudio"
            keyAlias = "playlistaudio"
            keyPassword = "playlistaudio"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("fixed")
        }
        debug {
            signingConfig = signingConfigs.getByName("fixed")
        }
    }

    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a") // chỉ build cho điện thoại 64-bit
            isUniversalApk = false
        }
    }

    // youtubedl-android chạy python/ffmpeg/quickjs từ nativeLibraryDir -> phải giải nén .so
    packaging {
        jniLibs { useLegacyPackaging = true }
    }

    // Bản engine dự phòng đóng gói trong APK (khi không tải được bản mới từ server)
    sourceSets["main"].assets.srcDirs("../engine", "../web")

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("io.github.junkfood02.youtubedl-android:library:$ytdlVersion")
    implementation("io.github.junkfood02.youtubedl-android:ffmpeg:$ytdlVersion")
    implementation("io.github.junkfood02.youtubedl-android:aria2c:$ytdlVersion")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.documentfile:documentfile:1.0.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
