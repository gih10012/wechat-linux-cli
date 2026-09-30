#define NCUT_ALLOW_HIGHLEVEL_SEND 1
#define NCUT_ALLOW_HIGHLEVEL_PREFLIGHT 1
#include "../src/wechat_linux_cli/_native/native_highlevel_helper.c"

static unsigned char app_object[16], services_object[16], manager_object[0x910];
static unsigned char request_object[0x610];
static int releases, requests, sends, result_destroys, recipient_assigns, text_assigns;
static int bad_manager;
static _Thread_local int active_context;
static int require_context;

static void fake_app(Shared *out) {
    out->object = app_object; out->control = app_object;
}
static void fake_services(Shared *out, void *app) {
    if (app != app_object) abort();
    out->object = services_object; out->control = services_object;
}
static void fake_manager(Shared *out, void *services) {
    if (require_context && !active_context) abort();
    if (services != services_object) abort();
    *(uintptr_t *)manager_object = bad_manager ? 1 : 0xa8bd418;
    *(void **)(manager_object + 0x8f8) = app_object;
    out->object = manager_object; out->control = manager_object;
}
static void fake_request(Shared *out, void *unused) {
    if (unused) abort();
    ++requests;
    memset(request_object, 0, sizeof(request_object));
    *(uintptr_t *)request_object = 0xa899f78;
    *(uint32_t *)(request_object + 0x7c) = 1;
    out->object = request_object; out->control = request_object;
}
static void fake_assign(void *dest, const void *bytes, size_t length) {
    if (dest == request_object + 0x90 && length == 10 &&
        !memcmp(bytes, "filehelper", length)) ++recipient_assigns;
    else if (dest == request_object + 0x5c8 && length == 5 &&
             !memcmp(bytes, "HELLO", length)) ++text_assigns;
    else abort();
}
static void fake_send(void *out, void *manager, const Shared *request) {
    if (manager != manager_object || request->object != request_object ||
        *(uint32_t *)(request_object + 0xe4) != 1) abort();
    ++sends;
    memset(out, 0, 0x30);
}
static void fake_result_destroy(void *out) {
    if (*(uint32_t *)out || *((uint32_t *)out + 1)) abort();
    ++result_destroys;
}
static void fake_shared_destroy(Shared *item) {
    if (!item->object || !item->control) abort();
    ++releases;
    item->object = item->control = NULL;
}

static void run_case(int send, int mismatched_manager, int expected_failure,
                     int expected_requests, int expected_sends, int expected_releases,
                     int bad_output) {
    char report_path[] = "/tmp/wechat-highlevel-fixture-report-XXXXXX";
    output_fd = bad_output ? open("/dev/full", O_WRONLY) : mkstemp(report_path);
    if (output_fd < 0) abort();
    releases = requests = sends = result_destroys = recipient_assigns = text_assigns = 0;
    worker_done = manager_verified = request_constructed = 0;
    submission_entered = result_returned = result_success = failure = 0;
    report_failed = 0;
    result_code0 = result_code1 = 0;
    bad_manager = mismatched_manager;
    should_send = send;
    payload[0] = 10; payload[1] = 0; payload[2] = 5; payload[3] = 0;
    memcpy(payload + 4, "filehelperHELLO", 15);
    payload_size = 19;
    perform_native();
    if (!worker_done || failure != expected_failure || requests != expected_requests ||
        sends != expected_sends || releases != expected_releases ||
        result_destroys != expected_sends ||
        recipient_assigns != expected_requests || text_assigns != expected_requests ||
        submission_entered != (bad_output ? 1 : expected_sends) ||
        result_returned != expected_sends ||
        result_success != expected_sends) abort();
    if (!bad_output) unlink(report_path);
}

static unsigned char dispatcher_object[32], job_object[16];
static TaskCallback *queued;
static TaskCallback inline_copy;
static int enqueue_calls, synchronous;
static void *fake_global_app(void) { return app_object; }
static void fake_dispatcher(Shared *out, void *app) {
    if (app != app_object) abort();
    *out = (Shared){dispatcher_object, dispatcher_object};
}
static void fake_enqueue(Shared *out, void *dispatcher, const SourceLocation *source,
                         TaskFunction *function, int flags) {
    if (dispatcher != dispatcher_object || flags != 1 || !source->file ||
        !source->function || source->line != 1) abort();
    ++enqueue_calls;
    if (synchronous) {
        active_context = 1;
        function->target->vtable->invoke(function->target);
        active_context = 0;
        function->target->vtable->delete_self(function->target);
        function->target = NULL;
        if (output_fd < 0 || !dispatch_pending) abort();
        *out = (Shared){job_object, job_object};
        return;
    }
    queued = function->target->vtable->clone(function->target);
    function->target->vtable->clone_into(function->target, &inline_copy);
    *out = (Shared){job_object, job_object};
}
static void *run_queued(void *unused) {
    (void)unused;
    active_context = 1;
    queued->vtable->invoke(queued);
    inline_copy.vtable->invoke(&inline_copy);
    active_context = 0;
    queued->vtable->delete_self(queued);
    return NULL;
}
static void run_dispatch_case(int cancel, int bad_output, int missing_scheduler) {
    char path[] = "/tmp/wechat-highlevel-dispatch-XXXXXX";
    output_fd = bad_output ? open("/dev/full", O_WRONLY) : mkstemp(path);
    if (output_fd < 0) abort();
    releases = requests = sends = enqueue_calls = 0;
    worker_done = manager_verified = request_constructed = failure = 0;
    submission_entered = result_returned = result_success = 0;
    live_callbacks = dispatch_pending = enqueue_returned = task_invoked = report_failed = 0;
    retained_dispatcher = (Shared){0};
    should_send = bad_manager = 0;
    require_context = 1;
    payload[0] = 10; payload[1] = 0; payload[2] = 5; payload[3] = 0;
    memcpy(payload + 4, "filehelperHELLO", 15); payload_size = 19;
    *(void **)(dispatcher_object + 0x10) = missing_scheduler ? NULL : app_object;
    dispatch_api = (DispatchApi){fake_global_app, fake_dispatcher, fake_enqueue};
    int code = enqueue_prepared();
    if (missing_scheduler || bad_output) {
        if (code != (missing_scheduler ? ENOTCONN : EIO) || enqueue_calls ||
            failure != (missing_scheduler ? 9 : 7) || !worker_done ||
            live_callbacks || output_fd != -1 || releases != 1) abort();
    } else if (synchronous) {
        if (code || !worker_done || requests != 1 || sends || failure ||
            live_callbacks || dispatch_pending || output_fd != -1 || releases != 6)
            abort();
    } else {
        if (code || worker_done || requests || live_callbacks != 2 || releases != 1)
            abort();
        if (cancel) queued->vtable->delete_self(queued);
        else {
            pthread_t thread;
            if (pthread_create(&thread, NULL, run_queued, NULL) ||
                pthread_join(thread, NULL)) abort();
            if (!worker_done || requests != 1 || sends || failure) abort();
        }
        /* A remaining inline clone must retain the dispatcher and report fd. */
        if (live_callbacks != 1 || output_fd < 0 || !retained_dispatcher.control)
            abort();
        inline_copy.vtable->destroy(&inline_copy);
        if (live_callbacks || !worker_done || output_fd != -1 ||
            retained_dispatcher.control || failure != (cancel ? 11 : 0) ||
            releases != (cancel ? 2 : 6)) abort();
    }
    require_context = 0;
    if (!bad_output) unlink(path);
}

int main(void) {
    image_base = 0;
    api = (NativeApi){fake_app, fake_services, fake_manager, fake_request,
                      fake_assign, fake_send, fake_result_destroy, fake_shared_destroy};
    run_case(0, 0, 0, 1, 0, 4, 0);
    run_case(1, 0, 0, 1, 1, 4, 0);
    run_case(1, 1, 4, 0, 0, 3, 0);
    run_case(1, 0, 7, 1, 0, 4, 1);
    run_dispatch_case(0, 0, 0);
    run_dispatch_case(1, 0, 0);
    run_dispatch_case(0, 1, 0);
    run_dispatch_case(0, 0, 1);
    synchronous = 1;
    run_dispatch_case(0, 0, 0);
    puts("highlevel fixture passed");
    return 0;
}
