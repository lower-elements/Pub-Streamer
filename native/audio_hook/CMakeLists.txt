cmake_minimum_required(VERSION 3.15)
project(pubstreamer_audio_hook C)

# Detect bitness and set the DLL output name accordingly.
if(CMAKE_SIZEOF_VOID_P EQUAL 8)
    set(ARCH_SUFFIX "64")
else()
    set(ARCH_SUFFIX "32")
endif()

set(DLL_NAME "audio_hook${ARCH_SUFFIX}")

add_library(${DLL_NAME} SHARED
    dllmain.c
    hook_engine.c
    hook_winmm.c
    hook_dsound.c
    hook_wasapi.c
)

target_compile_definitions(${DLL_NAME} PRIVATE
    WIN32_LEAN_AND_MEAN
    NOMINMAX
)

target_link_libraries(${DLL_NAME} PRIVATE
    winmm
    dsound
    kernel32
    ole32
    uuid
)


# Place output DLLs in a flat native/dist/ regardless of build config.
# Per-config variants are required because MSBuild (multi-config) ignores
# RUNTIME_OUTPUT_DIRECTORY and appends /Release or /Debug itself.
set_target_properties(${DLL_NAME} PROPERTIES
    OUTPUT_NAME                      "${DLL_NAME}"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/../dist"
    RUNTIME_OUTPUT_DIRECTORY_DEBUG   "${CMAKE_CURRENT_SOURCE_DIR}/../dist"
    LIBRARY_OUTPUT_DIRECTORY_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/../dist"
    LIBRARY_OUTPUT_DIRECTORY_DEBUG   "${CMAKE_CURRENT_SOURCE_DIR}/../dist"
)
