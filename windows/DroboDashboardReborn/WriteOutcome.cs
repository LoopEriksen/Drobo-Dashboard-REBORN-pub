using System.Text.Json;

namespace DroboDashboardReborn;

/// <summary>
/// How the agent said a configuration write ended.
///
/// Four cases, not two, because of what a real Drobo 5N did on 2026-08-25: it
/// was sent a share create, closed the connection without ever answering, and
/// had created the share anyway. Every layer above called that a failure. So a
/// write that applied must never be shown as failed, and a write nobody can
/// settle must never be shown as done -- the agent labels every write answer
/// with one of these (its "outcome" field) and this app renders the label
/// rather than inferring it from the status code or the wording.
/// </summary>
public enum WriteOutcome
{
    /// <summary>The Drobo acknowledged the write and it reads back changed.</summary>
    Confirmed,

    /// <summary>The Drobo never acknowledged the write, but it reads back
    /// changed anyway. A success — the read-back is what settles it, not the
    /// acknowledgement — shown green, carrying the agent's own account of the
    /// silence.</summary>
    ConfirmedUnacknowledged,

    /// <summary>The write went out and nothing could settle whether it applied.
    /// Shown as a WARNING, never an error: the change may already be on the
    /// device, and the agent's message says to go and look before trying
    /// again.</summary>
    Unsettled,

    /// <summary>Refused, unreachable, or applied nothing. A plain failure.</summary>
    Failed,
}

public static class WriteOutcomes
{
    /// <summary>
    /// Read the agent's "outcome" field out of a write answer.
    ///
    /// Defensive in one direction only. An answer with no "outcome" at all is
    /// judged the way this app judged every write before the field existed --
    /// by the HTTP status and "ok" -- and an outcome name this build does not
    /// recognise becomes Unsettled rather than a success, because that is the
    /// reading that cannot mislead: it costs a re-read, where a wrong success
    /// tells the owner a change is on their Drobo when it may not be.
    /// </summary>
    public static WriteOutcome Read(bool okStatus, JsonElement root)
    {
        var succeeded = okStatus
            && root.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True;
        var claimed = JsonHelpers.Str(root, "outcome", "") switch
        {
            "verified" => WriteOutcome.Confirmed,
            "verified_unacknowledged" => WriteOutcome.ConfirmedUnacknowledged,
            "failed" => WriteOutcome.Failed,
            "unknown" => WriteOutcome.Unsettled,
            "" => succeeded ? WriteOutcome.Confirmed : WriteOutcome.Failed,
            _ => WriteOutcome.Unsettled,
        };
        // A success label on an answer whose status says otherwise is a
        // contradiction, and the honest reading of a contradiction is that
        // nobody knows what happened.
        return !succeeded && claimed.Succeeded() ? WriteOutcome.Unsettled : claimed;
    }

    /// <summary>Whether the write is one the owner can treat as done.</summary>
    public static bool Succeeded(this WriteOutcome outcome) =>
        outcome is WriteOutcome.Confirmed or WriteOutcome.ConfirmedUnacknowledged;
}
