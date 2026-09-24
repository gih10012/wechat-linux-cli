#define NCUT_ALLOW_HIGHLEVEL_SEND 1
#include "../src/wechat_linux_cli/_native/native_highlevel_helper.c"

static unsigned char app_object[16], services_object[16], manager_object[0x910];
static unsigned char request_object[0x610];
static int releases, requests, sends, result_destroys, recipient_assigns, text_assigns;
static int bad_manager;

static void fake_app(Shared *out) {
    out->object = app_object; out->control = app_object;
}
static void fake_services(Shared *out, void *app) {
    if (app != app_object) abort();
    out->object = services_object; out->control = services_object;
}
static void fake_manager(Shared *out, void *services) {
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

int main(void) {
    image_base = 0;
    api = (NativeApi){fake_app, fake_services, fake_manager, fake_request,
                      fake_assign, fake_send, fake_result_destroy, fake_shared_destroy};
    run_case(0, 0, 0, 1, 0, 4, 0);
    run_case(1, 0, 0, 1, 1, 4, 0);
    run_case(1, 1, 4, 0, 0, 3, 0);
    run_case(1, 0, 7, 1, 0, 4, 1);
    puts("highlevel fixture passed");
    return 0;
}
