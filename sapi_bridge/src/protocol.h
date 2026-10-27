#pragma once

#include <cstdint>
#include <string>
#include <string_view>

struct RequestMetadata
{
    std::string bridgeProtocol;
    std::string engineInstanceId;
    std::string requestId;
};

struct AudioResponseMetadata
{
    std::uint64_t totalBytes = 0;
    std::uint64_t prefixBytes = 0;
    std::uint64_t speechBytes = 0;
};

std::string GenerateUuid();
RequestMetadata CreateRequestMetadata(const std::string &engineInstanceId);
std::wstring BuildProtocolRequestHeaders(const RequestMetadata &metadata);

bool TryParseUnsignedByteCount(std::wstring_view value, std::uint64_t &result);
AudioResponseMetadata ParseAudioResponseMetadata(
    std::wstring_view totalBytes,
    std::wstring_view prefixBytes,
    std::wstring_view speechBytes);
std::uint64_t CalculateProportionalAudioOffset(
    std::uint64_t speechBytes,
    std::uint64_t prefixBytes,
    std::uint32_t position,
    std::uint32_t span);
