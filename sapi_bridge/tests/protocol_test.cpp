#include "protocol.h"

#include <cstdint>
#include <iostream>
#include <limits>
#include <regex>
#include <string>
#include <vector>

namespace
{
int failures = 0;

void Check(bool condition, const char *message)
{
    if (!condition)
    {
        std::cerr << "FAIL: " << message << '\n';
        ++failures;
    }
}

void TestUuidFormatAndUniqueness()
{
    const std::regex uuidPattern(
        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");
    const std::string first = GenerateUuid();
    const std::string second = GenerateUuid();

    Check(std::regex_match(first, uuidPattern), "first UUID has canonical lowercase format");
    Check(std::regex_match(second, uuidPattern), "second UUID has canonical lowercase format");
    Check(first != second, "successive UUIDs are unique");
}

void TestExactRequestHeaders()
{
    const RequestMetadata metadata{
        "2",
        "11111111-2222-3333-4444-555555555555",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"};

    const std::wstring expected =
        L"X-VoiceLink-Bridge-Protocol: 2\r\n"
        L"X-VoiceLink-Engine-Instance: 11111111-2222-3333-4444-555555555555\r\n"
        L"X-VoiceLink-Request-Id: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\r\n";
    Check(BuildProtocolRequestHeaders(metadata) == expected,
          "request metadata produces the exact protocol-2 headers");
}

void TestUnsignedByteParsing()
{
    std::uint64_t parsed = 99;
    Check(TryParseUnsignedByteCount(L"0", parsed) && parsed == 0,
          "zero byte count is accepted");
    Check(TryParseUnsignedByteCount(L"18446744073709551615", parsed) &&
              parsed == (std::numeric_limits<std::uint64_t>::max)(),
          "maximum uint64 byte count is accepted");

    const std::vector<std::wstring> malformed = {
        L"", L"-1", L"+1", L" 1", L"1 ", L"1x", L"18446744073709551616"};
    for (const std::wstring &value : malformed)
    {
        parsed = 99;
        Check(!TryParseUnsignedByteCount(value, parsed) && parsed == 0,
              "malformed or overflowing byte count is rejected safely");
    }
}

void TestValidTimingMetadata()
{
    const AudioResponseMetadata metadata =
        ParseAudioResponseMetadata(L"1000", L"120", L"880");

    Check(metadata.totalBytes == 1000, "valid metadata preserves total bytes");
    Check(metadata.prefixBytes == 120, "valid metadata preserves prefix bytes");
    Check(metadata.speechBytes == 880, "valid metadata preserves speech bytes");
}

void TestMissingAndMalformedTimingFallback()
{
    const AudioResponseMetadata missing =
        ParseAudioResponseMetadata(L"1000", L"", L"");
    Check(missing.totalBytes == 1000 && missing.prefixBytes == 0 &&
              missing.speechBytes == 1000,
          "missing timing headers use legacy total-length behavior");

    const AudioResponseMetadata missingTotal =
        ParseAudioResponseMetadata(L"", L"120", L"880");
    Check(missingTotal.totalBytes == 0 && missingTotal.prefixBytes == 0 &&
              missingTotal.speechBytes == 0,
          "missing legacy total length leaves timing unknown");

    const AudioResponseMetadata malformed =
        ParseAudioResponseMetadata(L"1000", L"twelve", L"880");
    Check(malformed.totalBytes == 1000 && malformed.prefixBytes == 0 &&
              malformed.speechBytes == 1000,
          "malformed timing headers use legacy total-length behavior");

    const AudioResponseMetadata malformedSpeech =
        ParseAudioResponseMetadata(L"1000", L"120", L"eight-eighty");
    Check(malformedSpeech.totalBytes == 1000 && malformedSpeech.prefixBytes == 0 &&
              malformedSpeech.speechBytes == 1000,
          "malformed speech length uses legacy total-length behavior");

    const AudioResponseMetadata malformedTotal =
        ParseAudioResponseMetadata(L"-1", L"120", L"880");
    Check(malformedTotal.totalBytes == 0 && malformedTotal.prefixBytes == 0 &&
              malformedTotal.speechBytes == 0,
          "malformed legacy total length is ignored safely");
}

void TestInconsistentAndOverflowingTimingFallback()
{
    const AudioResponseMetadata inconsistent =
        ParseAudioResponseMetadata(L"1000", L"120", L"879");
    Check(inconsistent.totalBytes == 1000 && inconsistent.prefixBytes == 0 &&
              inconsistent.speechBytes == 1000,
          "inconsistent timing headers use legacy total-length behavior");

    const AudioResponseMetadata overflowing = ParseAudioResponseMetadata(
        L"18446744073709551615", L"18446744073709551615", L"1");
    Check(overflowing.totalBytes == (std::numeric_limits<std::uint64_t>::max)() &&
              overflowing.prefixBytes == 0 &&
              overflowing.speechBytes == (std::numeric_limits<std::uint64_t>::max)(),
          "prefix plus speech overflow cannot validate timing metadata");
}

void TestProportionalOffsetUsesSpeechAndAddsPrefix()
{
    Check(CalculateProportionalAudioOffset(800, 200, 2, 4) == 600,
          "word timing is proportional to speech bytes and offset by prefix bytes");
    Check(CalculateProportionalAudioOffset(
              (std::numeric_limits<std::uint64_t>::max)(), 0, 1, 1) ==
              (std::numeric_limits<std::uint64_t>::max)(),
          "proportional timing handles the maximum uint64 byte count safely");
    Check(CalculateProportionalAudioOffset(800, 200, 5, 4) == 1000,
          "positions beyond the text span are clamped safely");
}

void TestAudioChunkWriteProgressConsumesEveryAcceptedByte()
{
    AudioChunkWriteProgress progress{0, 100};

    Check(AdvanceAudioChunkWriteProgress(progress, 40) &&
              progress.offset == 40 && progress.remaining == 60,
          "short writes advance the pointer and retain the unaccepted suffix");
    Check(AdvanceAudioChunkWriteProgress(progress, 60) &&
              progress.offset == 100 && progress.remaining == 0,
          "successive writes consume the complete chunk");
}

void TestAudioChunkWriteProgressRejectsNoProgressAndOverReporting()
{
    AudioChunkWriteProgress progress{25, 75};

    Check(!AdvanceAudioChunkWriteProgress(progress, 0) &&
              progress.offset == 25 && progress.remaining == 75,
          "successful zero-byte writes are rejected without mutating progress");
    Check(!AdvanceAudioChunkWriteProgress(progress, 76) &&
              progress.offset == 25 && progress.remaining == 75,
          "writes cannot report more bytes than remain");

    const std::uint32_t maximum = (std::numeric_limits<std::uint32_t>::max)();
    AudioChunkWriteProgress overflowing{maximum, 1};
    Check(!AdvanceAudioChunkWriteProgress(overflowing, 1) &&
              overflowing.offset == maximum && overflowing.remaining == 1,
          "write progress rejects pointer-offset overflow");
}

void TestAudioChunkWriteProgressHandlesMaximumDwordChunk()
{
    const std::uint32_t maximum = (std::numeric_limits<std::uint32_t>::max)();
    AudioChunkWriteProgress progress{0, maximum};

    Check(AdvanceAudioChunkWriteProgress(progress, maximum - 1) &&
              progress.offset == maximum - 1 && progress.remaining == 1,
          "near-maximum chunk progress does not wrap");
    Check(AdvanceAudioChunkWriteProgress(progress, 1) &&
              progress.offset == maximum && progress.remaining == 0,
          "maximum DWORD chunk completes without overflow");
}

void TestRequestIdReusedAcrossRetries()
{
    const std::string engineId = GenerateUuid();
    const RequestMetadata metadata = CreateRequestMetadata(engineId);
    const std::string nextRequestId = CreateRequestMetadata(engineId).requestId;
    std::vector<std::wstring> retryHeaders;

    for (int attempt = 0; attempt < 3; ++attempt)
        retryHeaders.push_back(BuildProtocolRequestHeaders(metadata));

    Check(metadata.bridgeProtocol == "2", "new request metadata selects bridge protocol 2");
    Check(metadata.engineInstanceId == engineId, "request metadata preserves engine UUID");
    Check(!metadata.requestId.empty(), "request metadata creates a request UUID");
    Check(metadata.requestId != nextRequestId, "separate requests receive separate UUIDs");
    Check(retryHeaders[0] == retryHeaders[1] && retryHeaders[1] == retryHeaders[2],
          "simulated retries reuse one request ID");
}
} // namespace

int main()
{
    TestUuidFormatAndUniqueness();
    TestExactRequestHeaders();
    TestUnsignedByteParsing();
    TestValidTimingMetadata();
    TestMissingAndMalformedTimingFallback();
    TestInconsistentAndOverflowingTimingFallback();
    TestProportionalOffsetUsesSpeechAndAddsPrefix();
    TestAudioChunkWriteProgressConsumesEveryAcceptedByte();
    TestAudioChunkWriteProgressRejectsNoProgressAndOverReporting();
    TestAudioChunkWriteProgressHandlesMaximumDwordChunk();
    TestRequestIdReusedAcrossRetries();

    if (failures == 0)
    {
        std::cout << "All protocol tests passed\n";
        return 0;
    }

    std::cerr << failures << " protocol test(s) failed\n";
    return 1;
}
