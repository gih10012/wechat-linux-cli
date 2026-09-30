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
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <asm/prctl.h>
#include <sys/syscall.h>

#ifndef NCUT_ALLOW_HIGHLEVEL_SEND
#define NCUT_ALLOW_HIGHLEVEL_SEND 0
#endif
#ifndef NCUT_ALLOW_HIGHLEVEL_PREFLIGHT
#define NCUT_ALLOW_HIGHLEVEL_PREFLIGHT 0
#endif
#ifndef NCUT_ALLOW_HIGHLEVEL_DISPATCH
#define NCUT_ALLOW_HIGHLEVEL_DISPATCH 0
#endif

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
static atomic_int worker_done, manager_verified, request_constructed;
static atomic_int submission_entered, result_returned, result_success, failure;
static int live_callbacks, dispatch_pending;
static int report_failed;
static uint32_t result_code0, result_code1;
static unsigned char payload[2048];
static size_t payload_size;

/* The pinned manager getter dereferences FS-0x78 without a null check.
 * Read the active coroutine only; never fabricate or change client TLS. */
static int active_context_valid(const Shared *context) {
    if (!context || !context->object || !context->control) return 0;
    /* 4537200 cannot lock a control block whose strong-owner count is -1.
     * This is a rejection check, not a replacement for the client's lock. */
    return __atomic_load_n((intptr_t *)((unsigned char *)context->control + 8),
                           __ATOMIC_ACQUIRE) != -1;
}
static int native_context_available(void) {
    unsigned long fs_base = 0;
    if (syscall(SYS_arch_prctl, ARCH_GET_FS, &fs_base) || fs_base < 0x78)
        return 0;
    /* Match the client: it uses the thread self pointer read at FS:0,
     * then a Shared holder at self-0x78, not a raw coroutine object. */
    uintptr_t thread_self = *(uintptr_t *)fs_base;
    if (thread_self < 0x78) return 0;
    return active_context_valid(*(Shared **)(thread_self - 0x78));
}
static int (*context_available)(void) = native_context_available;

static int report_locked(void) {
    if (output_fd < 0) return 0;
    char json[512];
    int length = snprintf(json, sizeof(json),
        "{\"worker_done\":%s,\"live_callbacks\":%d,\"dispatch_pending\":%s,\"manager_verified\":%s,"
        "\"request_constructed\":%s,\"submission_entered\":%s,"
        "\"result_returned\":%s,\"result_success\":%s,"
        "\"result_code0\":%u,\"result_code1\":%u,\"failure\":%d}\n",
        worker_done && !dispatch_pending ? "true" : "false", live_callbacks,
        dispatch_pending ? "true" : "false", manager_verified ? "true" : "false",
        request_constructed ? "true" : "false", submission_entered ? "true" : "false",
        result_returned ? "true" : "false", result_success ? "true" : "false",
        result_code0, result_code1, failure);
    if (length <= 0 || (size_t)length >= sizeof(json) ||
        pwrite(output_fd, json, (size_t)length, 0) != length ||
        ftruncate(output_fd, length) != 0 || fsync(output_fd) != 0)
        report_failed = 1;
    if (worker_done && !live_callbacks && !dispatch_pending) {
        close(output_fd); output_fd = -1;
    }
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
    if (!context_available()) { failure = 14; goto release; }
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
    if (send && !NCUT_ALLOW_HIGHLEVEL_SEND) return ENOSYS;
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
    /* An idle OS thread has no guaranteed active client coroutine context.
     * Keep this synchronous route unavailable in production, independently
     * of the Python entrypoint, until a client scheduling route is verified.
     */
    if (!NCUT_ALLOW_HIGHLEVEL_PREFLIGHT) return ENOSYS;
    int code = initialize(base, data, length, result_path, send);
    if (code) return code;
    perform_native();
    return 0;
}

/* Pinned libc++ void() function ABI, matching the client's task vtable slots.
 * This candidate stays unavailable in production. Synthetic lifecycle checks
 * do not establish that the native scheduler installs the required context.
 */
typedef struct TaskCallback TaskCallback;
typedef struct {
    void (*destroy)(TaskCallback *);
    void (*delete_self)(TaskCallback *);
    TaskCallback *(*clone)(const TaskCallback *);
    void (*clone_into)(const TaskCallback *, TaskCallback *);
    void (*destroy_target)(TaskCallback *);
    void (*delete_target)(TaskCallback *);
    void (*invoke)(TaskCallback *);
    const void *(*target)(const TaskCallback *, const void *);
    const void *(*target_type)(const TaskCallback *);
} TaskCallbackTable;
struct TaskCallback { const TaskCallbackTable *vtable; };
typedef struct {
    _Alignas(16) unsigned char storage[32];
    TaskCallback *target;
} TaskFunction;
typedef struct {
    const char *file;
    const char *function;
    uint32_t line;
    uint32_t padding;
    const void *caller;
} SourceLocation;
_Static_assert(sizeof(TaskFunction) == 48, "libc++ task function size");
_Static_assert(__builtin_offsetof(TaskFunction, target) == 32, "task target offset");
_Static_assert(sizeof(SourceLocation) == 32, "native source location size");
_Static_assert(__builtin_offsetof(SourceLocation, caller) == 24, "caller PC offset");

typedef struct {
    void *(*global_app)(void);
    void (*dispatcher)(Shared *, void *);
    /* The final integer is rendered into the job label by 0x6243360.
     * Scheduling options are inherited separately from the scheduler. */
    void (*enqueue)(Shared *, void *, const SourceLocation *, TaskFunction *, int label_number);
    uintptr_t scheduler_vtable;
    uintptr_t coroutine_vtable;
} DispatchApi;
static DispatchApi dispatch_api;
static Shared retained_dispatcher;
static int enqueue_returned, task_invoked;

/* Release our dispatcher reference after enqueue returns and callback ownership
 * reaches zero. Native destruction runs outside our lock. The last destructor
 * is still executing here: neither worker_done nor dispatch_pending authorizes
 * unloading this helper, which must remain loaded until client exit. */
static Shared finish_dispatch_locked(void) {
    Shared release = {0};
    if (enqueue_returned && !live_callbacks) {
        if (!task_invoked) { if (!failure) failure = 11; worker_done = 1; }
        release = retained_dispatcher;
        retained_dispatcher = (Shared){0};
    }
    report_locked();
    return release;
}

static void finish_dispatch_release(Shared *release) {
    if (!release->control) return;
    api.shared_destroy(release);
    pthread_mutex_lock(&state_lock);
    dispatch_pending = 0;
    report_locked();
    pthread_mutex_unlock(&state_lock);
}

static void task_destroy(TaskCallback *self) {
    (void)self;
    pthread_mutex_lock(&state_lock);
    --live_callbacks;
    Shared release = finish_dispatch_locked();
    pthread_mutex_unlock(&state_lock);
    finish_dispatch_release(&release);
}
static void task_delete(TaskCallback *self) { task_destroy(self); free(self); }
static TaskCallback *task_clone(const TaskCallback *self) {
    TaskCallback *copy = malloc(sizeof(*copy));
    if (!copy) abort();
    *copy = *self;
    pthread_mutex_lock(&state_lock);
    ++live_callbacks;
    pthread_mutex_unlock(&state_lock);
    return copy;
}
static void task_clone_into(const TaskCallback *self, TaskCallback *copy) {
    *copy = *self;
    pthread_mutex_lock(&state_lock);
    ++live_callbacks;
    pthread_mutex_unlock(&state_lock);
}
static void task_invoke(TaskCallback *self) {
    (void)self;
    pthread_mutex_lock(&state_lock);
    int first = !task_invoked;
    task_invoked = 1;
    pthread_mutex_unlock(&state_lock);
    if (first) perform_native();
}
static const void *task_target(const TaskCallback *self, const void *type) {
    (void)self; (void)type; return NULL;
}
static const void *task_type(const TaskCallback *self) { (void)self; return NULL; }
static const TaskCallbackTable task_callbacks = {
    task_destroy, task_delete, task_clone, task_clone_into, task_destroy,
    task_delete, task_invoke, task_target, task_type
};

static int enqueue_prepared(void) {
    void *app = dispatch_api.global_app();
    if (!app) { failure = 8; worker_done = 1; report(); return ENOTCONN; }
    dispatch_api.dispatcher(&retained_dispatcher, app);
    if (!retained_dispatcher.object || !retained_dispatcher.control ||
        !*(void **)((unsigned char *)retained_dispatcher.object + 0x10)) {
        if (retained_dispatcher.control) api.shared_destroy(&retained_dispatcher);
        failure = 9; worker_done = 1; report(); return ENOTCONN;
    }
    /* Mirrors the pinned native dispatcher's 6241c10 -> 9ae6590 check.
     * The scheduler's +8 subobject carries cancellation at +0xa1.
     */
    void *scheduler = *(void **)((unsigned char *)retained_dispatcher.object + 0x10);
    void *coroutine = *(void **)retained_dispatcher.object;
    if (!coroutine || *(uintptr_t *)scheduler != dispatch_api.scheduler_vtable ||
        *(uintptr_t *)coroutine != dispatch_api.coroutine_vtable ||
        *(void **)((unsigned char *)scheduler + 0xf0) != coroutine) {
        api.shared_destroy(&retained_dispatcher);
        failure = 13; worker_done = 1; report(); return EPROTO;
    }
    if (*((unsigned char *)scheduler + 0xa9) & 1) {
        api.shared_destroy(&retained_dispatcher);
        failure = 12; worker_done = 1; report(); return ECANCELED;
    }
    TaskFunction function = {0};
    function.target = malloc(sizeof(*function.target));
    if (!function.target) {
        api.shared_destroy(&retained_dispatcher);
        failure = 10; worker_done = 1; report(); return ENOMEM;
    }
    function.target->vtable = &task_callbacks;
    pthread_mutex_lock(&state_lock);
    dispatch_pending = 1;
    ++live_callbacks;
    int persisted = report_locked();
    pthread_mutex_unlock(&state_lock);
    Shared job = {0};
    if (persisted) {
        const SourceLocation source = {"wechat-linux-cli", "highlevel_preflight", 1, 0, NULL};
        dispatch_api.enqueue(&job, retained_dispatcher.object, &source, &function, 1);
    } else {
        failure = 7;
    }
    if (function.target) task_delete(function.target);
    if (job.control) api.shared_destroy(&job);
    pthread_mutex_lock(&state_lock);
    enqueue_returned = 1;
    Shared release = finish_dispatch_locked();
    pthread_mutex_unlock(&state_lock);
    finish_dispatch_release(&release);
    return persisted ? 0 : EIO;
}

__attribute__((visibility("default")))
int ncut_highlevel_enqueue(uintptr_t base, const void *data, size_t length,
                           const char *result_path, int send) {
    if (!NCUT_ALLOW_HIGHLEVEL_DISPATCH || (send && !NCUT_ALLOW_HIGHLEVEL_SEND)) return ENOSYS;
    int code = initialize(base, data, length, result_path, send);
    if (code) return code;
    dispatch_api = (DispatchApi){
        .global_app = (void *)(base + 0x603c4e0),
        .dispatcher = (void *)(base + 0x603d470),
        .enqueue = (void *)(base + 0x62426e0),
        .scheduler_vtable = base + 0xaaaea98,
        .coroutine_vtable = base + 0xaaacf80
    };
    return enqueue_prepared();
}
