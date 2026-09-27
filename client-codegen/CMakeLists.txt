cmake_minimum_required(VERSION 3.16)
project(dash_client_codegen LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 11)
set(protobuf_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(protobuf_BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(protobuf_WITH_ZLIB OFF CACHE BOOL "" FORCE)
add_subdirectory("${SOURCES}/protobuf-3.18.1/cmake" protobuf)
foreach(language objective_c python)
  add_executable(grpc_${language}_plugin
    "${SOURCES}/grpc-1.46.3/src/compiler/${language}_plugin.cc"
    "${SOURCES}/grpc-1.46.3/src/compiler/${language}_generator.cc")
  target_include_directories(grpc_${language}_plugin PRIVATE "${SOURCES}/grpc-1.46.3" "${SOURCES}/grpc-1.46.3/include")
  target_link_libraries(grpc_${language}_plugin PRIVATE libprotoc libprotobuf)
  install(TARGETS grpc_${language}_plugin RUNTIME DESTINATION bin)
endforeach()
add_executable(protoc-gen-grpc-java
  "${SOURCES}/grpc-java-1.42.1/compiler/src/java_plugin/cpp/java_plugin.cpp"
  "${SOURCES}/grpc-java-1.42.1/compiler/src/java_plugin/cpp/java_generator.cpp")
target_compile_definitions(protoc-gen-grpc-java PRIVATE GRPC_VERSION=1.42.1)
target_link_libraries(protoc-gen-grpc-java PRIVATE libprotoc libprotobuf)
install(TARGETS protoc protoc-gen-grpc-java RUNTIME DESTINATION bin)
