namespace ApiGateway.RateLimiting;

[AttributeUsage(AttributeTargets.Method | AttributeTargets.Class, AllowMultiple = false)]
public class EnableRateLimitingAttribute : Attribute
{
    public string PolicyName { get; }
    public EnableRateLimitingAttribute(string policyName = "auth-strict") => PolicyName = policyName;
}
