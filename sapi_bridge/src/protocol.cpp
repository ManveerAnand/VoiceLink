#include "protocol.h"

#include <objbase.h>

#include <limits>

namespace
{
std::wstring WidenAscii(const std::string &value)
{
    return std::wstring(value.begin(), value.end());
}
} // namespace

std::string GenerateUuid()
{
    GUID guid = {};
    if (FAILED(CoCreateGuid(&guid)))
        return {};

    wchar_t guidText[39] = {};
    if (StringFromGUID2(guid, guidText, static_cast<int>(sizeof(guidText) / sizeof(guidText[0]))) == 0)
        return {};

    std::string result;
    result.reserve(36);
    for (int index = 1; index < 37; ++index)
    {
        wchar_t character = guidText[index];
        if (character >= L'A' && character <= L'F')
            character = static_cast<wchar_t>(character - L'A' + L'a');

        if ((character < L'0' || character > L'9') &&
            (character < L'a' || character > L'f') && character != L'-')
            return {};

        result.push_back(static_cast<char>(character));
    }
    return result;
}

RequestMetadata CreateRequestMetadata(const std::string &engineInstanceId)
{
    return {"2", engineInstanceId, GenerateUuid()};
}

std::wstring BuildProtocolRequestHeaders(const RequestMetadata &metadata)
{
    std::wstring headers;
    headers.reserve(128);
    headers += L"X-VoiceLink-Bridge-Protocol: ";
    headers += WidenAscii(metadata.bridgeProtocol);
    headers += L"\r\nX-VoiceLink-Engine-Instance: ";
    headers += WidenAscii(metadata.engineInstanceId);
    headers += L"\r\nX-VoiceLink-Request-Id: ";
    headers += WidenAscii(metadata.requestId);
    headers += L"\r\n";
    return headers;
}

bool TryParseUnsignedByteCount(std::wstring_view value, std::uint64_t &result)
{
    result = 0;
    if (value.empty())
        return false;

    constexpr std::uint64_t maximum = (std::numeric_limits<std::uint64_t>::max)();
    for (const wchar_t character : value)
    {
        if (character < L'0' || character > L'9')
        {
            result = 0;
            return false;
        }

        const std::uint64_t digit = static_cast<std::uint64_t>(character - L'0');
        if (result > (maximum - digit) / 10)
        {
            result = 0;
            return false;
        }
        result = result * 10 + digit;
    }
    return true;
}

AudioResponseMetadata ParseAudioResponseMetadata(
    std::wstring_view totalBytes,
    std::wstring_view prefixBytes,
    std::wstring_view speechBytes)
{
    AudioResponseMetadata metadata;
    if (!TryParseUnsignedByteCount(totalBytes, metadata.totalBytes))
        return {};

    metadata.speechBytes = metadata.totalBytes;

    std::uint64_t parsedPrefix = 0;
    std::uint64_t parsedSpeech = 0;
    if (TryParseUnsignedByteCount(prefixBytes, parsedPrefix) &&
        TryParseUnsignedByteCount(speechBytes, parsedSpeech) &&
        parsedPrefix <= metadata.totalBytes &&
        parsedSpeech == metadata.totalBytes - parsedPrefix)
    {
        metadata.prefixBytes = parsedPrefix;
        metadata.speechBytes = parsedSpeech;
    }

    return metadata;
}

std::uint64_t CalculateProportionalAudioOffset(
    std::uint64_t speechBytes,
    std::uint64_t prefixBytes,
    std::uint32_t position,
    std::uint32_t span)
{
    if (span == 0)
        return prefixBytes;

    const std::uint32_t clampedPosition = (position < span) ? position : span;
    const std::uint64_t whole =
        (speechBytes / span) * static_cast<std::uint64_t>(clampedPosition);
    const std::uint64_t fraction =
        ((speechBytes % span) * static_cast<std::uint64_t>(clampedPosition)) / span;
    const std::uint64_t proportional = whole + fraction;
    constexpr std::uint64_t maximum = (std::numeric_limits<std::uint64_t>::max)();
    if (prefixBytes > maximum - proportional)
        return maximum;
    return prefixBytes + proportional;
}
