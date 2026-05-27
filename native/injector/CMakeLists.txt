cmake_minimum_required(VERSION 3.15)
project(pubstreamer_injector C)

# This target must be compiled as 32-bit (x86).
# Run CMake with -A Win32 or via the x86 Developer Command Prompt.
add_executable(injector32 injector.c)

target_link_libraries(injector32 PRIVATE kernel32)

set_target_properties(injector32 PROPERTIES
    OUTPUT_NAME                      "injector32"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_SOURCE_DIR}/../dist"
    RUNTIME_OUTPUT_DIRECTORY_DEBUG   "${CMAKE_SOURCE_DIR}/../dist"
)
