
# test.tf

data "aws_lambda_invocation" "test_proxy_function" {
  function_name = aws_lambda_function.bedrock_proxy.function_name
  input         = file("${path.module}/test.json")
}

output "lambda_test_result" {
  value = data.aws_lambda_invocation.test_proxy_function.result
}

resource "null_resource" "test_assertion" {
  triggers = {
    response = data.aws_lambda_invocation.test_proxy_function.result
  }

  provisioner "local-exec" {
    command = "echo '${data.aws_lambda_invocation.test_proxy_function.result}' | findstr '200'"
  }
}
