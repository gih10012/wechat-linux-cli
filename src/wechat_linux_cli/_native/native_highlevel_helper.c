/* Experimental high-level text candidate for one SHA256-pinned Linux client.
 * GDB selects and verifies the observed event-loop LWP before this call.
 * No target text is logged.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct { void *object; void *control; } Shared;
typedef struct {
    void (*current_app)(Shared *);
    void (*services)(Shared *, void *);
    void (*manager)(Shared *, void *);
    void (*request)(Shared *, void *);
    void (*assign)(void *, const void *, size_t);
    void (*send)(void *, void *, const Shared *);
    void (*result_destroy)(void *);
    void (*shared_destroy)(Shared *);
} NativeApi;

static NativeApi api;
static uintptr_t image_base;
static pthread_mutex_t state_lock = PTHREAD_MUTEX_INITIALIZER;
static int started, output_fd = -1, should_send;
static int worker_done, manager_verified, request_constructed;
static int submission_entered, result_returned, result_success, failure;
static int report_failed;
static uint32_t result_code0, result_code1;
static unsigned char payload[2048];
static size_t payload_size;

static int report_locked(void) {
    if (output_fd < 0) return 0;
    char json[512];
    int length = snprintf(json, sizeof(json),
        "{\"worker_done\":%s,\"live_callbacks\":0,\"manager_verified\":%s,"
        "\"request_constructed\":%s,\"submission_entered\":%s,"
        "\"result_returned\":%s,\"result_success\":%s,"
        "\"result_code0\":%u,\"result_code1\":%u,\"failure\":%d}\n",
        worker_done ? "true" : "false", manager_verified ? "true" : "false",
        request_constructed ? "true" : "false", submission_entered ? "true" : "false",
        result_returned ? "true" : "false", result_success ? "true" : "false",
        result_code0, result_code1, failure);
    if (length <= 0 || (size_t)length >= sizeof(json) ||
        pwrite(output_fd, json, (size_t)length, 0) != length ||
        ftruncate(output_fd, length) != 0 || fsync(output_fd) != 0)
        report_failed = 1;
    if (worker_done) { close(output_fd); output_fd = -1; }
    return !report_failed;
}

static int report(void) {
    pthread_mutex_lock(&state_lock);
    int ok = report_locked();
    pthread_mutex_unlock(&state_lock);
    return ok;
}

static void perform_native(void) {
    Shared app = {0}, services = {0}, manager = {0}, request = {0};
    _Alignas(16) unsigned char result[0x30] = {0};
    api.current_app(&app);
    if (!app.object || !app.control) { failure = 2; goto release; }
    api.services(&services, app.object);
    if (!services.object || !services.control) { failure = 3; goto release; }
    api.manager(&manager, services.object);
    if (!manager.object || !manager.control ||
        *(uintptr_t *)manager.object != image_base + 0xa8bd418 ||
        !*(void **)((unsigned char *)manager.object + 0x8f8)) {
        failure = 4; goto release;
    }
    manager_verified = 1;
    report();

    api.request(&request, NULL);
    if (!request.object || !request.control ||
        *(uintptr_t *)request.object != image_base + 0xa899f78 ||
        *(uint32_t *)((unsigned char *)request.object + 0x7c) != 1) {
        failure = 5; goto release;
    }
    const size_t recipient_size = (size_t)payload[0] | ((size_t)payload[1] << 8);
    const size_t text_size = (size_t)payload[2] | ((size_t)payload[3] << 8);
    if (recipient_size == 0 || text_size == 0 ||
        recipient_size + text_size + 4 != payload_size) {
        failure = 6; goto release;
    }
    *(uint32_t *)((unsigned char *)request.object + 0xe4) = 1;
    api.assign((unsigned char *)request.object + 0x90, payload + 4, recipient_size);
    api.assign((unsigned char *)request.object + 0x5c8,
               payload + 4 + recipient_size, text_size);
    request_constructed = 1;
    report();

    if (should_send) {
        submission_entered = 1; /* Persist before the only possibly sending call. */
        if (!report()) { failure = 7; goto release; }
        api.send(result, manager.object, &request);
        result_returned = 1;
        memcpy(&result_code0, result, sizeof(result_code0));
        memcpy(&result_code1, result + 4, sizeof(result_code1));
        result_success = !result_code0 && !result_code1;
        report();
        api.result_destroy(result);
    }
release:
    if (request.control) api.shared_destroy(&request);
    if (manager.control) api.shared_destroy(&manager);
    if (services.control) api.shared_destroy(&services);
    if (app.control) api.shared_destroy(&app);
    memset(payload, 0, sizeof(payload));
    worker_done = 1;
    report();
}

static int initialize(uintptr_t base, const void *data, size_t length,
                      const char *result_path, int send) {
    if (!base || !data || length < 6 || length > sizeof(payload) ||
        (send != 0 && send != 1) ||
        strlen(result_path) >= PATH_MAX) return EINVAL;
    size_t recipient_size = (size_t)((const unsigned char *)data)[0] |
                            ((size_t)((const unsigned char *)data)[1] << 8);
    size_t text_size = (size_t)((const unsigned char *)data)[2] |
                       ((size_t)((const unsigned char *)data)[3] << 8);
    if (!recipient_size || !text_size || recipient_size + text_size + 4 != length)
        return EINVAL;
    if (!*(uintptr_t *)(base + 0xacef550)) return ENOTCONN;
    pthread_mutex_lock(&state_lock);
    if (started) { pthread_mutex_unlock(&state_lock); return EALREADY; }
    started = 1;
    pthread_mutex_unlock(&state_lock);
    image_base = base;
    api = (NativeApi){
        .current_app = (void *)(base + 0x603d3a0),
        .services = (void *)(base + 0x6198050),
        .manager = (void *)(base + 0x61a8af0),
        .request = (void *)(base + 0x4a01e80),
        .assign = (void *)(base + 0x450cf10),
        .send = (void *)(base + 0x64c7b40),
        .result_destroy = (void *)(base + 0x64c8940),
        .shared_destroy = (void *)(base + 0x4708170)
    };
    output_fd = open(result_path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (output_fd < 0) return errno;
    memcpy(payload, data, length);
    payload_size = length;
    should_send = send;
    if (!report()) { close(output_fd); output_fd = -1; return EIO; }
    return 0;
}

__attribute__((visibility("default")))
int ncut_highlevel_sync(uintptr_t base, const void *data, size_t length,
                        const char *result_path, int send) {
    int code = initialize(base, data, length, result_path, send);
    if (code) return code;
    perform_native();
    return 0;
}
