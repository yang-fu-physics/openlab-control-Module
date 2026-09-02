#ifndef OPENLAB_LABVIEW_ABI_H
#define OPENLAB_LABVIEW_ABI_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifdef _WIN32
#define OLC_API __declspec(dllexport)
#define OLC_CALL __cdecl
#else
#define OLC_API
#define OLC_CALL
#endif

typedef int32_t(OLC_CALL *OLC_JSON_FUNCTION)(
    const uint8_t *request_json,
    int32_t request_length,
    uint8_t *response_json,
    int32_t response_capacity,
    int32_t *response_length);

OLC_API int32_t OLC_CALL OLC_Describe(
    const uint8_t *request_json,
    int32_t request_length,
    uint8_t *response_json,
    int32_t response_capacity,
    int32_t *response_length);
OLC_API int32_t OLC_CALL OLC_Open(
    const uint8_t *request_json,
    int32_t request_length,
    uint8_t *response_json,
    int32_t response_capacity,
    int32_t *response_length);
OLC_API int32_t OLC_CALL OLC_Invoke(
    const uint8_t *request_json,
    int32_t request_length,
    uint8_t *response_json,
    int32_t response_capacity,
    int32_t *response_length);
OLC_API int32_t OLC_CALL OLC_Close(
    const uint8_t *request_json,
    int32_t request_length,
    uint8_t *response_json,
    int32_t response_capacity,
    int32_t *response_length);

#ifdef __cplusplus
}
#endif

#endif
